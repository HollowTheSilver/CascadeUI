# // ========================================( Modules )======================================== // #


import asyncio
import contextlib
import functools
import hashlib
import inspect
import logging
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, ClassVar, Dict, Optional, Set

import aiohttp
import discord
from discord import Interaction
from discord.ui import Button, Container
from discord.ui import File as UIFile
from discord.ui import Item, MediaGallery, Separator, TextDisplay, Thumbnail
from discord.ui.select import BaseSelect

from ..components.base import StatefulButton
from ..components.types import MAX_MESSAGE_CHARACTERS, EmojiInput
from ..exceptions import InstanceLimitError
from ..state.actions import ActionCreators
from ..state.singleton import get_store
from ..state.store import _CURRENT_INTERACTION
from ..utils.coercion import coerce_snowflake_id, coerce_snowflake_id_set, is_snowflake
from ..utils.hooks import await_maybe, call_hook_safe, is_async_callable
from ..utils.responses import DISCORD_CALL_ERRORS, ack_backstop, describe_discord_error
from ..utils.tasks import get_task_manager
from ._interaction import _ARMING_RETRY_SECONDS, _InteractionMixin
from ._navigation import _NavigationMixin

logger = logging.getLogger(__name__)

# Discord's custom_id ceiling. discord.py stores the value unchecked.
_CUSTOM_ID_MAX_CHARS = 100


# // ========================================( View Registry )======================================== // #


# Maps class name -> class for navigation stack resolution
_view_class_registry: Dict[str, type] = {}

# Class names already warned about an author locked out of their own view.
# Dedupes to once per class so repeat opens do not spam the log.
_user_id_lock_out_warned: set = set()

# Tracks view classes whose on_load() has overrun the interaction-timing budget.
# Deduped per class so a consistently-slow preload warns once, not on every
# send/navigation.
_slow_on_load_warned: set = set()

# Sibling of _slow_on_load_warned for the reactive rebuild seam (on_state_changed).
_slow_render_warned: set = set()

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

# Backoff for a rate-limit that arrives with no ``Retry-After`` to read.
#
# Reaching this constant means the bot is OFFLINE, not slow. discord.py
# absorbs and retries every ordinary rate-limit itself (it sleeps the
# response's own retry_after and re-sends), and raises a 429 only when the
# reply carries no ``Via`` header -- its own test for a request Cloudflare
# blocked at the edge, before Discord ever saw it. That is an IP-level ban
# against the whole bot, typically hour-scale: no message edits, no command
# responses, and not even an interaction ack, since acks are HTTP too.
#
# So this is not a UI pacing value and there is no interactive latency to
# trade against -- every view is already dead when it is read. It governs
# one thing: how hard a banned bot keeps knocking. 429s count toward the
# same invalid-response budget that triggers the ban, so retrying every
# second can hold the door shut. Minutes-scale is the point; the only cost
# is staying quiet a little past a ban that lifted early.
_CLOUDFLARE_BAN_BACKOFF = 60.0


def _class_path(cls) -> str:
    """Return the import path (``module.QualName``) that identifies a class.

    The key `_view_class_registry` is built on, and the value navigation
    entries record so `pop()` can resolve them. Distinct from
    `_class_session_key()`, which names a session *family* and may be shared
    by two classes: anything reconstructing a specific class reads this.
    """
    return f"{cls.__module__}.{cls.__qualname__}"


def _register_view_class(cls):
    """Auto-register view classes for nav stack class resolution.

    Keyed by the fully-qualified class path so sibling modules can reuse
    short class names without clobbering each other in the registry.
    """
    _view_class_registry[_class_path(cls)] = cls


# // ========================================( Render Outcome )======================================== // #


class RenderOutcome(str, Enum):
    """What a render call did with the edit it was asked to ship.

    Returned by :meth:`_StatefulMixin.refresh` and relayed by
    :meth:`_StatefulMixin.reload`, so a caller that changed something before
    rendering can tell a shipped edit from one that never left, and a reload
    that ran from one the throttle handed to a scheduled task. Members compare
    equal to their string values (``outcome == "deferred"``), so a caller can
    branch without importing the enum.

    - ``RENDERED``: the edit reached Discord.
    - ``SKIPPED``: the tree matches the last shipped render, so no edit was
      owed; the screen already shows this state.
    - ``DEFERRED``: no edit has shipped yet; a scheduled task re-runs the
      render when the active cooldown or rate-limit window closes.
    - ``DROPPED``: the edit was attempted and is not known to have landed (a
      transport failure, or a request stalled past ``edit_timeout``), and
      nothing is scheduled to retry it. The definitively-dropped case also
      reports through :attr:`_StatefulMixin.refresh_degraded`; a stalled
      request is indeterminate, so only the disposition covers it.
    - ``NO_MESSAGE``: no editable message remains. The view has not been
      sent, the message was deleted, or an ephemeral's webhook token has
      expired. Retrying cannot help.
    """

    RENDERED = "rendered"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    DROPPED = "dropped"
    NO_MESSAGE = "no_message"

    def __str__(self) -> str:
        # str() of a str-mixin enum member differs across the supported
        # interpreter range; pinning it to the value keeps log and f-string
        # output stable everywhere.
        return self.value


def _merge_reload_kwargs(pending: dict, new: dict) -> dict:
    """Combine a coalesced reload's kwargs into the already-pending set.

    Boolean values OR across the calls, so a ``force=True`` request survives
    an unforced reload landing later in the same window; any other value
    takes the newest call's, since the single replay stands in for the last
    state asked for. Replacing the dict wholesale would silently drop the
    earlier call's ``force=True``.
    """
    merged = dict(pending)
    for key, value in new.items():
        previous = merged.get(key)
        if isinstance(previous, bool) and isinstance(value, bool):
            merged[key] = previous or value
        else:
            merged[key] = value
    return merged


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

    # Subclass config: instance limiting
    instance_limit: Optional[int] = None  # None = unlimited
    instance_scope: str = "user_guild"  # "user", "guild", "user_guild", "global"
    instance_policy: str = "replace"  # "replace" or "reject"
    # Optional override for the default ephemeral message sent when a user
    # hits the instance limit.  Falsy values fall back to
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

    @property
    def parent(self):
        """The view this one is attached to, or ``None``.

        Read-only. A child constructed with ``parent=`` reads it before the
        send that attaches it, and every child reads it after. Reach for this
        rather than storing the same view a second time under a name of your
        own: a child panel that needs its parent to read state or call
        ``respond`` already has it here.

        Mutation goes through ``attach_child`` on the parent, which enforces
        the invariants a plain assignment cannot (no self-attachment, no
        cycles, clean re-parenting).
        """
        return self._attached_to or self._pending_parent

    # Subclass config: auto-defer safety net
    auto_defer: bool = True
    # Seconds before the auto-defer timer acks an unresponded interaction. The
    # ceiling is Discord's 3s interaction wall, measured from interaction
    # CREATION (not callback dispatch), so gateway latency already eats into it.
    # Two ack-coupled EDIT budgets derive from this by -1.0 -- the acting-view
    # fast-path timeout and _ack_bounded -- so lowering it for ack headroom also
    # shrinks the in-place edit window. A starved event loop delays the timer
    # regardless, so keep hot-path I/O off the loop rather than retuning this.
    auto_defer_delay: float = 2.5
    # Opt-in: ack the interaction before the access checks and the callback run,
    # so a callback that synchronously blocks the loop still lands its ack. Costs
    # the acting-view one-call refresh fast path (every refresh takes two calls),
    # and a callback on an ack_first view cannot open a modal: open_modal needs
    # the un-acked response slot, so after the early ack it falls back to an
    # ephemeral message instead.
    ack_first: bool = False

    # Subclass config: refresh throttling
    # When set to a positive int, enforces a minimum gap (in milliseconds)
    # between successful message edits on this view. Refreshes that land
    # inside the window are deferred via a single scheduled task that
    # re-enters ``on_state_changed`` once the window expires, so the edit
    # reflects the latest store state rather than whatever was current at
    # the deferred call's site. ``None`` (default) disables the proactive
    # cooldown. Independent of the always-on reactive 429 backoff, which
    # arms its own window and is never waived.
    refresh_cooldown_ms: Optional[int] = None

    # Subclass config: edit timeout ceiling
    # discord.py issues HTTP edits with no total timeout (aiohttp defaults to
    # total=None), so a connection that stalls without a response would pin
    # the awaiting code -- and, on the interaction-locked refresh/navigation
    # paths, the view itself -- until the socket drops. This ceiling bounds
    # every live-view and teardown edit through ``_bounded``: a stall is
    # cancelled after this many seconds and the view recovers on the next
    # interaction. The default clears realistic attachment uploads while
    # still capping a true hang. ``None`` disables the ceiling (unbounded
    # awaits, matching discord.py's own default). The acting-view fast path
    # keeps its own tighter bound, which protects the 3s ack deadline rather
    # than guarding against a hang.
    edit_timeout: Optional[float] = 60.0

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
    # This attribute is the author's declaration; the library never
    # assigns to it (see ``_refresh_handoff`` for how the effective
    # policy resolves). Default ``None`` means "derive from ``timeout``
    # at each ephemeral send()": any view with ``timeout=None`` or
    # ``timeout > 900`` engages the handoff (the view wants to outlive
    # the 900s webhook cliff); anything ``<= 900`` skips it (the token
    # outlives the view). The declared ``timeout`` is never rewritten --
    # the resolution is the only thing the library decides. Explicit
    # ``True`` or ``False`` overrides the derivation. A push/pop
    # destination is honored the same way against the chain's arming
    # deadline: ``True`` engages even when the send declined, ``False``
    # stays off, and ``None`` inherits the immediate source's effective
    # policy.
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
    # only: exit() calls are governed by exit_policy, and on_timeout
    # always freezes regardless of either policy.
    replace_policy: str = "delete"
    # Static message sent to the channel when this view is replaced and
    # has active participants. ``None`` (default) means silent replacement.
    # For dynamic UX (mentions, embeds, logging), override the
    # ``on_replaced`` method instead.
    replaced_message: Optional[str] = None

    # Default for exit() calls that pass no explicit delete_message
    # argument. "disable" (default) freezes the components in place,
    # matching the historical safe-by-default behavior; "delete" removes
    # the message. Explicit delete_message arguments to exit() always
    # override this policy. This governs the close buttons built by
    # make_exit_button / add_exit_button / make_nav_row, which forward
    # delete_message=None by default, plus any other site that calls
    # exit() without specifying delete_message. on_timeout is NOT
    # governed by this policy: an expiry is not a close gesture, so a
    # timed-out view always freezes rather than deleting content the
    # user never asked to dismiss.
    exit_policy: str = "disable"

    # Mention parsing for this view's own message. None (default) defers
    # to the bot's client-level AllowedMentions, which discord.py already
    # threads into every send path. Set it when the rendered body carries
    # user or role mentions that should not notify (a leaderboard, a
    # roster, a turn announcement), since the declaration then applies to
    # the initial send and to every refresh, on every endpoint. Not to be
    # confused with allowed_users, which is access control (Pillar 1);
    # this governs Discord payload formatting only.
    allowed_mentions: Optional[discord.AllowedMentions] = None

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
    # Attributes that must be a positive float/int or None (None = disabled).
    _OPTIONAL_POSITIVE_NUMBER_ATTRS: ClassVar[tuple] = ("edit_timeout",)
    # Attributes that must be a bool.
    _BOOL_ATTRS: ClassVar[tuple] = (
        "owner_only",
        "auto_defer",
        "ack_first",
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
    # Button label / emoji triples. Patterns extend these the same way they
    # extend _BUTTON_STYLE_ATTRS, so all three members of a button's triple
    # are checked at class-definition time rather than only the style.
    _STR_OR_NONE_ATTRS: ClassVar[tuple] = ("refresh_button_label",)
    _EMOJI_ATTRS: ClassVar[tuple] = ("refresh_button_emoji",)
    # ``str.format`` templates, mapped to the keyword arguments the render
    # site supplies. The values are test arguments of the render-time type,
    # not bare placeholder names: a format spec can be valid for one type
    # and not another (``{n:.2f}`` accepts a float and rejects a str), so
    # only the real type proves the template renders.
    _FORMAT_ATTRS: ClassVar[dict] = {}
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
        enum_attrs = cls._effective_table("_ENUM_ATTRS")
        if name in enum_attrs:
            allowed = enum_attrs[name]
            if value not in allowed:
                raise ValueError(
                    f"{cls.__name__}.{name} must be one of "
                    f"{sorted(a for a in allowed if a is not None)!r}, got {value!r}"
                )
            return
        if name in cls._effective_table("_POSITIVE_INT_ATTRS"):
            if value is None:
                return
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"{cls.__name__}.{name} must be a positive int or None, got {value!r}"
                )
            return
        if name in cls._effective_table("_POSITIVE_NUMBER_ATTRS"):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{cls.__name__}.{name} must be a positive number, got {value!r}")
            return
        if name in cls._effective_table("_OPTIONAL_POSITIVE_NUMBER_ATTRS"):
            if value is None:
                return
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"{cls.__name__}.{name} must be a positive number or None, got {value!r}"
                )
            return
        if name in cls._effective_table("_BOOL_ATTRS"):
            if not isinstance(value, bool):
                raise ValueError(
                    f"{cls.__name__}.{name} must be a bool, got {type(value).__name__}"
                )
            return
        if name in cls._effective_table("_OPTIONAL_BOOL_ATTRS"):
            if value is None:
                return
            if not isinstance(value, bool):
                raise ValueError(
                    f"{cls.__name__}.{name} must be a bool or None, " f"got {type(value).__name__}"
                )
            return
        if name in cls._effective_table("_BUTTON_STYLE_ATTRS"):
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
        if name in cls._effective_table("_STR_OR_NONE_ATTRS"):
            cls._validate_str_or_none(name, value)
            return
        if name in cls._effective_table("_EMOJI_ATTRS"):
            cls._validate_emoji(name, value)
            return
        if name in cls._effective_table("_FORMAT_ATTRS"):
            cls._validate_format(name, value)
            return
        if name == "nav_rebuild":
            if value is None:
                return
            # The mistake worth catching is a value that binds as a method
            # in a class body, so it receives self instead of the
            # destination view. Descriptor-ness decides that, not
            # callability: a staticmethod object is callable too, so
            # callable() cannot see the difference. staticmethod is the one
            # descriptor that is correct here, since binding through it
            # returns the wrapped callable untouched.
            #
            # functools.partial is named separately because it became a
            # descriptor in 3.14 and is not one on 3.10-3.13. Rejecting it
            # only where it binds would let the same class definition pass
            # on one supported Python and fail on another; rejecting it
            # everywhere means staticmethod is the answer on all of them.
            binds = hasattr(type(value), "__get__") or isinstance(value, functools.partial)
            if binds and not isinstance(value, staticmethod):
                raise TypeError(
                    f"{cls.__name__}.nav_rebuild is a "
                    f"{type(value).__name__}, which binds as a method in a "
                    f"class body: it would be called with self as its first "
                    f"argument instead of the destination view.\n"
                    f"  Fix: nav_rebuild = staticmethod(...)"
                )
            if not callable(value):
                raise TypeError(
                    f"{cls.__name__}.nav_rebuild must be callable or None, "
                    f"got {type(value).__name__}."
                )
            return
        if name == "allowed_mentions":
            if value is None:
                return
            if not isinstance(value, discord.AllowedMentions):
                raise TypeError(
                    f"{cls.__name__}.allowed_mentions must be a "
                    f"discord.AllowedMentions instance or None, "
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
        definition time* (at module import) instead of failing
        silently or surfacing as a confusing runtime error deep inside
        the dispatch loop. Only attributes the subclass actually
        overrode (present in ``cls.__dict__``) are checked, so inherited
        defaults pay zero cost.
        """
        own = cls.__dict__
        for table, check in (
            ("_ENUM_ATTRS", cls._validate_attribute_value),
            ("_POSITIVE_INT_ATTRS", cls._validate_attribute_value),
            ("_POSITIVE_NUMBER_ATTRS", cls._validate_attribute_value),
            ("_OPTIONAL_POSITIVE_NUMBER_ATTRS", cls._validate_attribute_value),
            ("_BOOL_ATTRS", cls._validate_attribute_value),
            ("_OPTIONAL_BOOL_ATTRS", cls._validate_attribute_value),
            ("_BUTTON_STYLE_ATTRS", cls._validate_attribute_value),
            ("_STR_OR_NONE_ATTRS", cls._validate_str_or_none),
            ("_EMOJI_ATTRS", cls._validate_emoji),
            ("_FORMAT_ATTRS", cls._validate_format),
        ):
            for attr in cls._effective_table(table):
                if attr in own:
                    check(attr, own[attr])
        if "subscribed_actions" in own:
            cls._validate_attribute_value("subscribed_actions", own["subscribed_actions"])
        if "theme" in own:
            cls._validate_attribute_value("theme", own["theme"])
        if "allowed_mentions" in own:
            cls._validate_attribute_value("allowed_mentions", own["allowed_mentions"])
        if "nav_rebuild" in own:
            cls._validate_attribute_value("nav_rebuild", own["nav_rebuild"])
        if "scoped_slot" in own:
            cls._validate_attribute_value("scoped_slot", own["scoped_slot"])
        if "persistent_slots" in own:
            cls._validate_attribute_value("persistent_slots", own["persistent_slots"])
        if "scoped_slot" in own or "persistent_slots" in own:
            cls._validate_slot_coherence()

    @classmethod
    def _effective_table(cls, name: str) -> tuple:
        """Union every declaration of a validation table across the MRO.

        A pattern mixin extends a table by splatting the base it knows
        about (``*_StatefulMixin._BOOL_ATTRS``), and that mixin precedes
        the concrete V1/V2 class in the MRO, so reading the table as a
        plain attribute returns the mixin's copy and silently drops
        whatever the concrete class added. ``validate_placement`` is
        declared on ``StatefulLayoutView``, which is exactly the position
        that loses. Unioning the whole MRO makes the extension idiom safe
        regardless of which base each mixin happened to name.
        """
        merged: dict = {}
        # Reversed so a nearer class's entry wins on collision, matching
        # normal attribute resolution. Tables are declared as tuples of
        # names or as name -> allowed-values dicts; a dict carries its
        # values through so the caller can look them up from the same
        # merged view it tested membership against.
        for klass in reversed(cls.__mro__):
            declared = klass.__dict__.get(name)
            if declared is None:
                continue
            if isinstance(declared, dict):
                merged.update(declared)
            else:
                for attr in declared:
                    merged.setdefault(attr, None)
        return merged

    @classmethod
    def _validate_str_or_none(cls, name: str, value) -> None:
        """Reject a button label that is neither a string nor ``None``."""
        if value is not None and not isinstance(value, str):
            raise TypeError(
                f"{cls.__name__}.{name} must be a str or None, got {type(value).__name__}"
            )

    @classmethod
    def _validate_format(cls, name: str, value) -> None:
        """Reject a format template that cannot render at its call site.

        The template is rendered once against the keyword arguments the
        render site supplies, so a typo'd or unbalanced placeholder fails
        where the class is defined instead of inside a click callback,
        where it surfaces as a bare ``KeyError`` naming neither the
        attribute nor the fix. ``str.format`` reports four different
        exception types for the four ways a template can be wrong, so the
        catch is broad on purpose.
        """
        if value is None:
            return
        if not isinstance(value, str):
            raise TypeError(
                f"{cls.__name__}.{name} must be a str or None, got {type(value).__name__}"
            )
        test_kwargs = cls._effective_table("_FORMAT_ATTRS")[name] or {}
        try:
            value.format(**test_kwargs)
        except Exception as exc:
            allowed = ", ".join("{" + key + "}" for key in test_kwargs)
            raise ValueError(
                f"{cls.__name__}.{name} is not a valid format template "
                f"({type(exc).__name__}: {exc}). Valid placeholders: {allowed}."
            ) from exc

    @classmethod
    def _validate_emoji(cls, name: str, value) -> None:
        """Reject a button emoji outside the union discord.py accepts."""
        if value is not None and not isinstance(value, (str, discord.Emoji, discord.PartialEmoji)):
            raise TypeError(
                f"{cls.__name__}.{name} must be a str, discord.Emoji, "
                f"discord.PartialEmoji, or None, got {type(value).__name__}"
            )

    @classmethod
    def _validate_slot_coherence(cls) -> None:
        """Catch the ``scoped_slot`` / ``persistent_slots`` copy-paste footgun.

        A view with ``scoped_slot = "my_stats"`` writes scoped data to the
        ``my_stats`` bucket. Declaring ``persistent_slots = ("scoped",)``
        alongside that custom slot means the view persists the default
        bucket, which nothing writes to, while ``my_stats`` remains
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
        # Validated before the class is registered anywhere, so a definition
        # that is going to be refused is never left resolvable by a later
        # lookup. Subclass hooks further down the chain read the pin to key
        # their own registries, which is why its shape is settled first.
        cls._validate_session_class_key()
        cls._validate_class_attributes()
        _register_view_class(cls)

        # A selector runs inline inside dispatch, where nothing can await it.
        # An async override is therefore never resolved: the store compares
        # one fresh coroutine against the last, they never match, and the
        # notification is skipped every time -- so the view goes silently
        # deaf to state while looking correctly wired. ``subscribe`` refuses
        # an async selector where it is handed one, but it is handed the
        # lambda ``_build_selector`` wraps this method in, and a lambda is
        # never a coroutine function whatever it closes over, so the
        # override is checked at the class that declares it.
        own_selector = cls.__dict__.get("state_selector")
        if own_selector is not None and is_async_callable(own_selector):
            raise TypeError(
                f"{cls.__name__}.state_selector must be synchronous; it is compared "
                f"inline during dispatch, which cannot await it, so an async one is "
                f"never resolved and the view stops receiving state updates."
                f"\n  Fix: read the slice from the state argument and return it "
                f"directly. Do async work in on_state_changed."
            )

        # Register declared persistent slot names with the module-level
        # set the middleware checks. Sticky by design -- once any class
        # declares a slot name persistent, every ``access_slot`` write to
        # that name is flushed regardless of which reducer performs it.
        slots = cls.__dict__.get("persistent_slots")
        if slots:
            from ..state.slots import _PERSISTENT_SLOTS

            for slot_name in slots:
                _PERSISTENT_SLOTS.add(slot_name)

        # Wrap __init__ so kwargs are auto-captured for push/pop
        # reconstruction. The test is whether this class already inherits a
        # wrapper, not whether it declares __init__ itself: a pattern can
        # take its __init__ from a plain mixin that never triggers
        # __init_subclass__ (the _Base*Mixin classes do not subclass
        # _StatefulMixin), and keying off cls.__dict__ skipped those
        # entirely -- they captured nothing, so pop rebuilt them with none
        # of their constructor arguments and a popped form came back with
        # no fields. Only the outermost wrapper captures; inner ones skip
        # via the _pending_init_kwargs guard.
        resolved_init = cls.__init__
        if not getattr(resolved_init, "_cascadeui_captures_kwargs", False):
            original_init = resolved_init

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
                try:
                    original_init(self, *args, **kw)
                except BaseException:
                    # ``_StatefulMixin.__init__`` subscribes the view to the
                    # store and may register it for undo before the subclass
                    # body runs. A body that raises leaves both behind holding
                    # a strong reference to a view no caller ever receives, so
                    # neither is collected and a broad ``state_selector`` would
                    # keep rendering it. Both removals are idempotent, so a
                    # nested wrapper cleaning up first costs nothing.
                    view_id = getattr(self, "id", None)
                    store = getattr(self, "state_store", None)
                    if view_id is not None and store is not None:
                        store._unsubscribe(view_id)
                        store._undo_enabled_views.pop(view_id, None)
                    raise

            _capturing_init._cascadeui_captures_kwargs = True
            cls.__init__ = _capturing_init

        # Wrap build_ui to set the theme context automatically and
        # stabilize auto-generated custom_ids after the tree is built.
        # Builder functions like card() and stats_card() read the theme
        # context as a fallback when no explicit color= is passed.
        # Stable custom_ids prevent the ViewStore dispatch race described
        # on ``_stabilize_custom_ids``.
        if "build_ui" in cls.__dict__:
            original_build = cls.build_ui

            # Two wrappers because the sync one must stay sync: three classes
            # call ``self.build_ui()`` from ``__init__``, which cannot await.
            # ``iscoroutinefunction`` picks between them, and it answers False
            # for shapes that still return a coroutine -- a callable instance
            # whose ``__call__`` is async, a ``functools.partial`` around one,
            # a plain function that returns one. Those take the sync branch,
            # so it checks the result rather than trusting the dispatch: with
            # a coroutine in hand the body has not run yet, and stabilizing
            # ids or dropping the ambient theme at that point would be doing
            # the wrapper's work against a tree that does not exist.
            if inspect.iscoroutinefunction(original_build):

                @functools.wraps(original_build)
                async def _themed_build_ui(self, *args, **kw):
                    from ..theming.context import theme_context

                    with theme_context(self.get_theme()):
                        result = await original_build(self, *args, **kw)
                        self._stabilize_custom_ids()
                        return result

            else:

                @functools.wraps(original_build)
                def _themed_build_ui(self, *args, **kw):
                    from ..theming.context import theme_context

                    with theme_context(self.get_theme()):
                        result = original_build(self, *args, **kw)
                        if not inspect.isawaitable(result):
                            self._stabilize_custom_ids()
                            return result

                    # Hand back a coroutine that re-enters the theme for the
                    # body and stabilizes once it has actually built. The
                    # context is entered again rather than held open across
                    # the yield: it is a contextvar, so the second entry costs
                    # a set and a reset, and holding one open across an await
                    # would leak the theme into whatever else the awaiting
                    # task runs.
                    async def _finish_themed_build():
                        with theme_context(self.get_theme()):
                            value = await result
                            self._stabilize_custom_ids()
                            return value

                    return _finish_themed_build()

            cls.build_ui = _themed_build_ui

        # on_load is the modern preload seam and builds component trees
        # the same way build_ui does, so it gets the same ambient theme.
        # Custom_id stabilization is not repeated here -- refresh() and
        # the send pipeline already stabilize at their own seams.
        if "on_load" in cls.__dict__:
            original_load = cls.on_load

            @functools.wraps(original_load)
            async def _themed_on_load(self, *args, **kw):
                from ..theming.context import theme_context

                with theme_context(self.get_theme()):
                    # The wrapper is always a coroutine function because the
                    # library awaits ``on_load`` at three render seams, but the
                    # override it wraps need not be: a preload with no I/O
                    # reads naturally as a plain ``def``.
                    return await await_maybe(original_load(self, *args, **kw))

            cls.on_load = _themed_on_load

    @classmethod
    def _validate_session_class_key(cls) -> None:
        """Settle the pin's shape before anything keys a registry on it.

        A non-string reaches the backend as the ``view_class`` column, where
        SQLite's TEXT affinity stores an int as its digits and the lookup
        then misses forever, and asyncpg refuses the bind outright. An empty
        string is worse than wrong: the truthiness read in
        ``_class_session_key`` falls back to the class path, so the attribute
        is set and never applied.

        Called from ``__init_subclass__`` for every view, and again by the
        persistent registration before it uses the pin as a dict key,
        where an unhashable value would otherwise raise from inside the
        lookup naming neither the attribute nor the fix.
        """
        if "session_class_key" not in cls.__dict__:
            return
        pin = cls.__dict__["session_class_key"]
        if not isinstance(pin, str) or not pin:
            raise ValueError(
                f"{cls.__name__}.session_class_key must be a non-empty string "
                f"naming the class identity to pin; got {pin!r}."
                f"\n  Fix: use the class path its stored rows already hold, "
                f'e.g. session_class_key = "mybot.views.TicketPanel".'
            )

    @classmethod
    def _class_session_key(cls) -> str:
        """Identifier for this view class's session family.

        Returns the class path (``module.QualName``) by default, which the
        Python import system guarantees unique across a running process.
        Discriminates session IDs, the instance index, session origin
        tracking, and the ``view_class`` column persistent registry rows
        store. Navigation entries record `_class_path` instead: `pop()`
        reconstructs the exact class that pushed, and a family may hold two.

        Subclasses set a ``session_class_key`` class attribute to override
        it. The load-bearing use is a persistent view whose class moves or
        is renamed: stored rows resolve only against the name they recorded,
        so pinning the old one is what keeps those panels reattaching. It
        can also unify two classes into one session family, which is rare
        and never safe for persistent classes -- they share one registry
        slot, so rows reattach to whichever class was defined last. The
        override is read via ``cls.__dict__`` so it does not inherit: each
        class opts in for itself.
        """
        override = cls.__dict__.get("session_class_key")
        if override:
            return override
        return _class_path(cls)

    def __init__(self, *args, **kwargs):
        # Extract custom arguments before passing to View/LayoutView
        self.state_store = kwargs.pop("state_store", None) or get_store()
        self.session_id = kwargs.pop("session_id", None)
        self.user_id = kwargs.pop("user_id", None)
        # Whether user_id was explicitly supplied (vs framework-derived below
        # from the interaction/context). Drives the divergence warning after
        # coercion so a consumer repurposing this reserved kwarg for their own
        # data is flagged at the point of the mistake.
        _user_id_supplied = self.user_id is not None
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
        # Monotonic deadline for the ephemeral refresh-button arming, stamped
        # at every ephemeral send. It records when the send token's 900s
        # window closes; whether the handoff acts on it is
        # auto_refresh_ephemeral's call. Lets a re-schedule after a failed
        # navigation rollback sleep the REMAINING time, not a fresh window.
        self._ephemeral_arm_deadline = None
        # Library-side resolution of the auto_refresh_ephemeral ``None``
        # sentinel: derived from ``timeout`` at each ephemeral send,
        # inherited from the immediate source on push/pop. Kept apart from
        # the declaration, which only ever holds what the class or the
        # caller set -- an explicit declaration wins over this value (see
        # ``_refresh_handoff``). ``None`` until something resolves it.
        self._refresh_handoff_resolved: Optional[bool] = None

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
        # Set by ``refresh`` when it swallowed a transport failure, so a
        # caller that needs the edit to have LANDED can tell that apart from
        # a refresh that returned normally. A repaint is content to retry on
        # the next state change; navigation is not, because it tears the
        # source down on the strength of the edit having shipped.
        self._refresh_degraded: bool = False

        # Whether state registration has been done
        self._registered = False

        # Session origin: when a view is pushed via navigation, this is set to
        # the root view's class name so the entire nav chain is tracked under
        # one instance index key.  None means this view IS the root.
        self._instance_root_class: Optional[str] = None

        # View-local navigation stack.  On push, the new view receives
        # parent._nav_stack + [entry_for_parent].  On pop, the restored
        # view receives current._nav_stack[:-1].  Each view owns its own
        # breadcrumb trail independently.
        self._nav_stack: list = []

        # Participants: non-owner users registered in the instance index.
        # Used by multi-user views (games, collaborative tools) so that
        # instance limiting applies to all participants, not just the owner.
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
        # One warning per view: the seams that check it run on every edit.
        self._text_budget_warned: bool = False
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

        # Refresh throttling state: two monotonic timestamps, each marking
        # the earliest moment the next edit may ship, kept apart because
        # they answer to different authorities. ``_cooldown_not_before`` is
        # the library's own opt-in pacing (written after a successful edit
        # when ``refresh_cooldown_ms`` is set) and is waived for edits made
        # in direct response to an interaction. ``_ratelimit_not_before`` is
        # Discord's, armed by the reactive 429 path, and is waived for
        # nothing -- exempting it would hammer an endpoint that has already
        # said stop. Collapsing the two into one field makes the second
        # guarantee impossible to keep. ``_deferred_refresh_task`` holds the
        # single pending retry, so N refreshes inside the window produce one
        # scheduled task, not N.
        self._cooldown_not_before: float = 0.0
        self._ratelimit_not_before: float = 0.0
        self._deferred_refresh_task: Optional[asyncio.Task] = None
        # Set when reload() is called inside a cooldown window so the single
        # deferred task re-fetches (via reload) at the boundary instead of a
        # plain on_state_changed re-render: coalescing a burst of out-of-band
        # reloads into one on_load fetch.
        self._reload_pending: bool = False
        # Keyword args of the reload() that got coalesced, replayed by the
        # deferred boundary task so a forwarded reload keyword (e.g. force)
        # survives the defer.
        self._pending_reload_kwargs: dict = {}
        # Reload serialization primitives (one reload's on_load + render
        # at a time); see reload() for the full contract and the
        # reentrancy raise.
        self._reload_lock = asyncio.Lock()
        self._reload_task: Optional[asyncio.Task] = None

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

        # Recorded for _warn_if_locked_out, which runs at send once the
        # subclass __init__ has finished and allowed_users exists.
        self._explicit_user_id = _user_id_supplied

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
        # Resolved through the MRO, not the leaf __dict__, so a value declared
        # on a base class is inherited; copied into a fresh per-instance set so
        # sibling instances never share a mutable. Default is an empty set (no
        # notifications); None opts in to all actions.
        if not hasattr(type(self), "subscribed_actions"):
            self.subscribed_actions: Optional[Set[str]] = set()
        else:
            declared = type(self).subscribed_actions
            self.subscribed_actions = None if declared is None else set(declared)

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

    def _warn_if_locked_out(self) -> None:
        """Warn once per class when a non-user ``user_id`` locks out its author.

        ``user_id`` names the view's owner, and the framework reads it for
        access control, session derivation, and instance scoping. An id that
        addresses something other than a Discord user is read the same way,
        and the access check then measures every clicker against a value none
        of them can match.

        Two conditions have to agree before this says anything, because either
        one alone describes working code. Naming an owner who is not the
        clicker is supported (the game examples hand the challenger ownership
        from the opponent's Accept click, and an admin who posts a panel for
        somebody else is doing the same thing on purpose), so a locked-out
        author proves nothing by itself. And an application's own identifier is
        inert until something reads it, so an id that fails
        :func:`~cascadeui.utils.is_snowflake` proves nothing either. Together
        they describe one thing only: an id that cannot name a Discord user,
        sitting where the access check reads it, locking out the author.

        The pairing leaves one shape quiet. An id that is not a snowflake still
        keys the instance index, so a view that sets ``instance_limit`` and
        admits its author through ``allowed_users`` scopes every user onto one
        key while this stays silent. That shape surfaces on its own: under the
        default ``instance_policy`` the second user to open the view replaces
        the first.

        Two reasons this runs from the send pipeline and nowhere else.
        ``allowed_users`` is conventionally assigned after ``super().__init__()``
        returns, so the question has no answer at the construction site. And
        ``_navigate_to`` supplies ``user_id`` as an explicit kwarg, which makes
        every pushed child of a divergent-owner parent look explicit too:
        checking there would warn once per class all the way down the nav tree.
        The root view answers for itself at its own send; its children inherit
        the owner and stay quiet.
        """
        if not self._explicit_user_id:
            return
        if self.interaction is None or self.user_id is None:
            return
        if is_snowflake(self.user_id):
            return
        author = self.interaction.user.id
        if author == self.user_id:
            return

        # Mirrors interaction_check's own order: allowed_users overrides
        # owner_only, so the branch that rejects is the one to name.
        if self.allowed_users is not None:
            blocker = "allowed_users does not include them"
            locked_out = author not in self.allowed_users
        elif self.owner_only:
            blocker = "owner_only admits only user_id"
            locked_out = True
        else:
            return
        if not locked_out:
            return

        cls_name = type(self).__qualname__
        if cls_name in _user_id_lock_out_warned:
            return
        _user_id_lock_out_warned.add(cls_name)
        logger.warning(
            f"{cls_name} was built by user {author} with user_id={self.user_id}, "
            f"which is not a Discord ID, and {author} cannot interact with the "
            f"result ({blocker}). user_id names the view's owner and is read for "
            f"access control, session identity, and instance scoping. Give "
            f"application data a kwarg name of its own."
        )

    def _build_selector(self):
        """Build a selector function if the subclass overrides state_selector.

        Returns None if state_selector is not overridden (base implementation),
        which means the subscriber receives all matching notifications.

        The view's theme rides along with whatever the subclass selects. A
        theme is a render input that appears in no view's data, so a selector
        tracking content alone never sees a theme change and paints on with a
        stale accent. A fixed theme yields a constant key and notifies no more
        often than before; a view that sets its own colors rebuilds once and
        stops at the render hash, shipping no edit.
        """
        # Only use a selector if the subclass actually overrides state_selector
        if type(self).state_selector is not _StatefulMixin.state_selector:
            return lambda state: (self.state_selector(state), self._theme_key())
        return None

    def _theme_key(self):
        """Identity of the theme this view renders with now, or None.

        ``get_theme`` is a render hook and takes no state argument, so a
        dynamic override reads the live store. That holds here because the
        store evaluates selectors against ``self.state`` itself, making the
        live read and the selector's argument the same object. A raising
        override degrades to None rather than poisoning the comparison.

        The name, not the ``Theme``: ``get_theme`` falls back to a bare
        ``Theme("fallback")`` built on each call, and the class defines no
        ``__eq__``, so comparing objects reports a change on every dispatch
        for any view without a registered theme.
        """
        try:
            theme = self.get_theme()
        except Exception:
            return None
        return getattr(theme, "name", None)

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
                session_id=self.session_id, user_id=self.user_id, guild_id=self.guild_id
            )
            await self.state_store.dispatch("SESSION_CREATED", payload)

        # Register the view
        payload = ActionCreators.view_created(
            view_id=self.id,
            view_type=self.__class__.__name__,
            user_id=self.user_id,
            session_id=self.session_id,
            guild_id=self.guild_id,
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

    async def _send_defer_timer(self, ephemeral: bool) -> None:
        """Defer a slow send's interaction before the 3s wall (N3 backstop).

        The send pipeline's pre-ack stages (on_pre_send, on_load, instance
        enforcement, seeding) run on the interaction clock when send() is
        entered from a slash command. Defers with the pending send's ephemeral
        flag so a followup after this ack renders in the right visibility.
        Cancelled before the send when the pre-send work finishes in time.
        """
        if self.interaction is None:
            return
        await ack_backstop(
            self.interaction,
            self.auto_defer_delay,
            owner=type(self).__name__,
            log=logger,
            ephemeral=ephemeral,
        )

    async def _carry_undo_stacks_to(self, new_view) -> None:
        """Move this view's undo/redo timeline onto a successor.

        Call between the successor's registration and this view's
        teardown, while both state rows exist. Routed through a
        ``VIEW_UPDATED`` dispatch so the transfer runs through the reducer
        rather than writing into the live ``state["views"]`` row in place.
        """
        old_view_state = self.state_store.state.get("views", {}).get(self.id, {})
        stack_updates = {}
        if old_view_state.get("undo_stack"):
            stack_updates["undo_stack"] = list(old_view_state["undo_stack"])
        if old_view_state.get("redo_stack"):
            stack_updates["redo_stack"] = list(old_view_state["redo_stack"])
        if stack_updates:
            await new_view.dispatch(
                "VIEW_UPDATED",
                ActionCreators.view_updated(new_view.id, **stack_updates),
            )

    def _carry_participants_to(self, new_view) -> None:
        """Move this view's participants onto a successor.

        Call after the successor is registered. The membership guard keeps
        it idempotent against participants the successor already claimed
        for itself.
        """
        for pid in self._participants:
            if pid not in new_view._participants:
                new_view._participants.add(pid)
                self.state_store._register_participant(new_view, pid)

    def _carry_attachments_to(self, new_view) -> None:
        """Move this view's parent and child links onto a successor.

        Without the hand-off, this view's ``exit()`` cascades into
        ``_cleanup_attached_children`` and deletes children that should
        outlive the swap, and a parent still tracking this view skips the
        successor as untracked, leaving an orphan panel behind.

        Destructive on this view's own tracking, so callers run it only
        once the swap is confirmed: a rolled-back navigation must find the
        source still holding its children and its parent link. The child
        list is snapshotted because ``attach_child`` prunes it while
        re-parenting.
        """
        for child in list(self._attached_children):
            if child is new_view:
                # The successor taking over the message supersedes its old
                # child link; attach_child rejects self-attachment.
                continue
            new_view.attach_child(child)
        parent = self._attached_to
        if parent is not None and not parent.is_finished():
            parent.attach_child(new_view)
            try:
                parent._attached_children.remove(self)
            except ValueError:
                pass
        self._attached_to = None

    async def _rollback_send(self, *, registered: bool) -> None:
        """Undo everything a failed ``send()`` had built so far.

        ``__init__`` creates the store subscriber and the undo-tracking
        entry before ``send()`` ever runs, so every abort path owes their
        removal. ``registered`` names the depth reached: ``False`` for a
        failure before the view entered the registries (the pre-send veto,
        an instance-limit rejection), ``True`` once ``_register_state`` has
        dispatched, which also owes the task cancellation and the
        ``VIEW_DESTROYED`` teardown.
        """
        self.stop()
        # Unconditional: on_load runs at Stage 0a, before the instance-limit
        # gate, so a view whose preload spawned a task owns one even on the
        # paths that never reached the registries. Cancelling for an owner
        # with no tasks costs nothing.
        self.task_manager.cancel_tasks(self.id)
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        # The attach never happened: it is the last stage of a successful
        # send. Left set, `parent` would name a view that does not track
        # this one and will not tear it down.
        self._pending_parent = None
        if registered:
            await self.state_store._destroy_view(self.id, source_id=self.id)

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
                call. Must include ``view=self``. Both versions may add
                ``file`` / ``files`` / ``allowed_mentions``; V1 also adds
                ``content`` / ``embed`` / ``embeds``, which V2 has no
                parameters for.
            ephemeral: Whether the message should be ephemeral.

        Returns:
            The sent ``discord.Message`` on success, or ``None`` when a
            policy gate blocked the send.
        """
        # Diagnostics only, and cheap: the subclass __init__ has returned by
        # now, so allowed_users exists and the reserved-user_id question can
        # finally be answered. Never blocks the send.
        self._warn_if_locked_out()

        send_defer_task = None
        # -- Stage 0: pre-send gate --
        # on_pre_send() is the public veto hook: a permission or data check
        # that runs FIRST, before any preload, placement walk, state
        # mutation, or Discord call. Returning False aborts cleanly: no
        # message ships and no state registers. It runs while the interaction
        # response slot is still open, so an override can respond() to explain
        # the veto -- and, when it proceeds, the slot stays available for the
        # actual send (no forced defer). Default returns True.
        # The N3 backstop must stand down on EVERY exit from the pre-send
        # stages -- the early returns (veto, instance limit, participant
        # rejection) and any raise, not only the normal fall-through -- or the
        # timer fires a phantom defer after send() already returned None. The
        # try/finally guarantees the single cancel site covers all of them.
        try:
            if not await await_maybe(self.on_pre_send(self.interaction)):
                await self._rollback_send(registered=False)
                return None

            # N3: arm a send-scoped ack backstop AFTER the veto. The genuinely
            # slow pre-send stages (on_load, instance enforcement, seed) run on
            # the 3s interaction clock, and a slow on_load would expire the
            # interaction before Stage 5 acks. Arming after on_pre_send keeps
            # that hook's documented open response slot (an override may respond
            # raw); a component-callback send is already covered by
            # _scheduled_task's timer. The finally below stands it down on
            # every exit.
            if (
                self.auto_defer
                and self.interaction is not None
                and not self.interaction.response.is_done()
            ):
                send_defer_task = asyncio.create_task(self._send_defer_timer(ephemeral))

            # -- Stage 0a: async preload --
            # on_load() is the public hook where a view fetches from a
            # database or other async source and builds its tree against the
            # result. It runs before placement validation and the Discord
            # send so the first render reflects loaded data, not the
            # synchronous-__init__ placeholder. Default is a no-op, so views
            # without async preload pay nothing. Matches where the built-in
            # pattern overrides (leaderboard, paginated cursor mode) already
            # preload -- before the tree is validated and shipped.
            await self._run_on_load()

            # -- Stage 1: instance enforcement --
            try:
                await self._enforce_instance_limit()
            except InstanceLimitError as e:
                # The rollback runs in a finally because raising from the
                # override is a documented shape: the default hook itself
                # re-raises when there is no interaction to answer on. Without
                # it, a re-raise leaves the rejected view subscribed and never
                # stopped, which is permanent for timeout=None.
                try:
                    await await_maybe(self.on_instance_limit(e))
                finally:
                    await self._rollback_send(registered=False)
                return None

            # -- Stage 2+3: state registration and participant claiming --
            # Batched so SESSION_CREATED + VIEW_CREATED collapse into one
            # BATCH_COMPLETE. On participant-rejection rollback, the queued
            # VIEW_DESTROYED joins the same batch and the whole self-cancelling
            # sequence fires as a single notification, so this batch's own
            # notification never carries a transient "view exists" state. It
            # bounds what this batch reports, not what a dispatch from another
            # task reads: reducers commit inline, so a concurrent notification
            # sees live state mid-sequence. ``source_id`` threads this view
            # through so its initial ``on_state_changed`` awaits inline and the
            # first render lands flush with the send response.
            async with self.state_store.batch(source_id=self.id):
                self.state_store._register_view(self)
                await self._register_state()

                try:
                    # Seed hook fires after registration so the view exists in
                    # state, but inside the batch so any seeding dispatches join
                    # the same BATCH_COMPLETE notification. Subscribers see the
                    # seeded slot from frame one. Default is a no-op.
                    await await_maybe(self.seed_initial_state(self.state_store.state))
                except BaseException:
                    # BaseException, not Exception: a cancellation landing here
                    # skips the rollback just as surely as a raise does, and
                    # leaves the same registered view with no message.
                    # Registration already happened, and this stage sits above
                    # the send's own rollback, so a raising hook would leave a
                    # registered view with no message: invisible to the user,
                    # counted by the instance limit, and never torn down. Under
                    # instance_policy="reject" that locks the owner out of the
                    # view class for the life of the process. The rollback's
                    # dispatches join this batch, so the whole sequence is
                    # net-zero and the abort has nothing left to announce.
                    await self._rollback_send(registered=True)
                    raise

                if type(self).auto_register_participants:
                    if not await self._auto_register_participants():
                        await self._rollback_send(registered=True)
                        return None

            # -- Stage 4: ephemeral refresh-handoff resolution --
            # auto_refresh_ephemeral is the author's declaration and is never
            # assigned here (see _refresh_handoff for the read-time
            # precedence). None re-derives against the declared timeout on
            # every send and lands on _refresh_handoff_resolved; the 900s
            # threshold is the webhook token cliff, so a view that wants to
            # live past it needs the handoff and one that does not skips it.
            # _ephemeral tracks the current send unconditionally, so an instance
            # reused for a non-ephemeral send (a replace() destination that
            # inherited a stale True) does not keep the flag set. The handoff
            # resolution runs only for an ephemeral send: a public send has no
            # webhook cliff to outlive.
            self._ephemeral = ephemeral
            if ephemeral and self.auto_refresh_ephemeral is None:
                self._refresh_handoff_resolved = self.timeout is None or self.timeout > 900
        finally:
            # Pre-send stages are done (or bailed). If the work overran, the
            # timer already fired and Stage 5 routes through followup; otherwise
            # cancel it before it fires.
            if send_defer_task is not None and not send_defer_task.done():
                send_defer_task.cancel()

        # -- Stage 5: Discord send --
        # Capture any caller-supplied attachments before the send so the
        # rollback path can close their underlying file pointers if the
        # send raises before discord.py's own ``finally`` runs (e.g.
        # validation failures inside discord.py reject the payload before
        # the HTTP layer is reached, and the file objects opened by the
        # caller are otherwise leaked).
        # Bound before the try because the handler below reads it: filling it
        # is what can raise, so an empty list has to exist first or the
        # rollback fails on an unbound name instead of running.
        files_to_close: list = []

        class_name = type(self).__name__
        try:
            # Filled inside the try because filling it can raise: a caller
            # passing one File to files= reaches an iteration of a
            # non-iterable, and outside the try that strands the view in
            # both registries with no message and no teardown, which under
            # instance_policy="reject" locks its own author out for the life
            # of the process.
            if send_kwargs.get("file") is not None:
                files_to_close.append(send_kwargs["file"])
            if send_kwargs.get("files"):
                files_to_close.extend(send_kwargs["files"])
            # Pre-flight validation runs HERE, on the final tree -- after
            # seed_initial_state has built it -- so the check sees exactly what
            # ships to Discord. Catches duplicate custom_ids (V1/V2) and invalid
            # V2 placements before the HTTP send. A rejection rolls back the full
            # registration via the teardown below, since state registration has
            # already happened by this stage.
            self._apply_theme_defaults()
            self._sync_back_buttons()
            self._check_placement()
            self._warn_unmatched_attachment_refs(send_kwargs)
            # The parent attach itself lands after the send, where a raise
            # would report a failure for a message Discord already has. The
            # chain is knowable now, so it is judged now.
            if self._pending_parent is not None:
                self._pending_parent._check_attachment(self)

            if self.context and hasattr(self.context, "send"):
                if ephemeral:
                    send_kwargs["ephemeral"] = ephemeral
                message = await self.context.send(**send_kwargs)

            elif self.interaction:
                send_kwargs["ephemeral"] = ephemeral
                if not self.interaction.response.is_done():
                    try:
                        await self.interaction.response.send_message(**send_kwargs)
                    except discord.HTTPException as e:
                        # 40060: a cancelled send-scoped defer (N3) landed
                        # server-side in the narrow ack-window race, acking the
                        # slot after the is_done() check returned False. Ship via
                        # followup instead of rolling the send back.
                        if getattr(e, "code", None) != 40060:
                            raise
                        message = await self.interaction.followup.send(**send_kwargs, wait=True)
                    else:
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
            await self._rollback_send(registered=True)
            raise

        # -- Stage 6: message re-fetch for token-free editing --
        if not ephemeral and isinstance(
            message, (discord.InteractionMessage, discord.WebhookMessage)
        ):
            self._webhook_message = message
            try:
                self._message = await message.channel.fetch_message(message.id)
            except DISCORD_CALL_ERRORS:
                # RateLimited and aiohttp's transport errors are siblings of
                # HTTPException, not subclasses. Uncaught, any of them would
                # escape a send that already succeeded, leaving the view with
                # no _message: no cleanup listener, no parent attach, and a
                # failure reported for a live message.
                self._message = message
        else:
            self._message = message

        # Everything below is bookkeeping against a message Discord has
        # already accepted, so it carries the same contract the re-fetch
        # above states: a failure here must not surface as a failed send.
        # The rollback path is behind us, so a raise would leave the view
        # registered and the message live while telling the caller neither
        # happened -- and a caller that retries on that answer posts a
        # second copy. Failures degrade and are logged instead.
        try:
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
                bot = getattr(self.interaction, "client", None) or getattr(
                    self.context, "bot", None
                )
                if isinstance(bot, discord.Client):
                    self.state_store._install_message_cleanup(bot)

            if ephemeral:
                # Stamped for every ephemeral send, since the deadline is a
                # fact about the message's token clock, not about whether
                # this view acts on it -- the timer below runs only when the
                # effective handoff policy engaged.
                self._ephemeral_arm_deadline = time.monotonic() + max(
                    1, 900 - self.refresh_warning_seconds
                )
                if self._refresh_handoff:
                    self.create_task(self._schedule_ephemeral_refresh())

            if self._pending_parent is not None:
                self._pending_parent.attach_child(self)
                self._pending_parent = None
        except Exception as e:
            # Which step failed is unknown by the time this catches, so the
            # digest is dropped rather than trusted: None means "no baseline"
            # and the next refresh ships unconditionally, where a stale
            # baseline would skip an edit the view needs.
            self._last_tree_digest = None
            logger.error(
                f"{type(self).__name__} was sent, but post-send setup raised "
                f"{type(e).__name__}: {e}. The message is live and the view is "
                f"registered; deletion cleanup, the ephemeral refresh handoff, "
                f"or the parent attachment may be inactive on it.",
                exc_info=e,
            )

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
            await await_maybe(self.on_unauthorized(interaction))
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
            await self.respond(interaction, self.unauthorized_message, ephemeral=True)
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not send unauthorized response in {self.__class__.__name__}: {describe_discord_error(e)}"
            )

    async def _call_hook_safe(self, hook, *args) -> None:
        """Run a fire-and-forget user hook, logging any exception.

        An override that raises must never block the surrounding navigation,
        rebuild, or lifecycle flow. Shared by every pattern that fires a
        post-event hook (``on_page_changed``, ``on_tab_switched``,
        ``on_step_entered`` / ``on_step_exited``, ``on_field_changed``,
        ``on_replaced``).
        """
        await call_hook_safe(hook, *args, owner=type(self).__name__, log=logger)

    async def on_instance_limit(self, error: "InstanceLimitError") -> None:
        """Called when ``send()`` is blocked by the instance limit.

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
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"Could not send instance limit response in {self.__class__.__name__}: {describe_discord_error(e)}"
                )
            return

        if self.context is not None and hasattr(self.context, "send"):
            try:
                await self.context.send(message, ephemeral=True)
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"Could not send instance limit response in {self.__class__.__name__}: {describe_discord_error(e)}"
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
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not send participant limit response in {self.__class__.__name__}: {describe_discord_error(e)}"
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
            except DISCORD_CALL_ERRORS as e:
                logger.debug(
                    f"Could not send replaced notification in {self.__class__.__name__}: {describe_discord_error(e)}"
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
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not send error response in {self.__class__.__name__}: {describe_discord_error(e)}"
            )

    async def on_reopen_failure(
        self, interaction: Interaction, error: Exception | None = None
    ) -> None:
        """Called when the ephemeral refresh button fails to spawn a replacement.

        Only fires for ephemeral views whose refresh handoff engaged (an
        explicit ``auto_refresh_ephemeral = True``, or the default ``None``
        resolving to engaged from a ``timeout`` past the 900s cliff).
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
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not send reopen failure response in {self.__class__.__name__}: {describe_discord_error(e)}"
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

    @staticmethod
    def _fit_custom_id(anchor: str) -> str:
        """Bring a generated anchor under Discord's 100-character cap.

        A qualname from a factory closure plus an 80-character label can
        overrun the cap on its own. Raising is not an option here (this
        runs inside every ``build_ui`` and a rebuild must not fail on a
        label the user is allowed to set), so the overflow is folded into
        a digest of the full anchor instead. Deterministic within a
        process, which is all the dispatch table needs; these ids are
        per-instance and never expected to survive a restart.
        """
        if len(anchor) <= _CUSTOM_ID_MAX_CHARS:
            return anchor
        digest = hashlib.blake2s(anchor.encode(), digest_size=6).hexdigest()
        return f"{anchor[: _CUSTOM_ID_MAX_CHARS - len(digest) - 1]}~{digest}"

    def _build_ui_sync(self) -> None:
        """Run ``build_ui`` from a caller that cannot await it.

        Three patterns compose their tree in ``__init__`` -- a synchronous
        frame with no way to resolve a coroutine. An async ``build_ui``
        there ran nothing: the class constructed, the tree stayed empty,
        and the only signal was a "never awaited" warning that most bots
        never surface. The mistake then arrived as a placement error at
        send naming "no top-level components", which describes the symptom
        and points nowhere near the cause.

        Checks the result rather than the function, so an object with an
        async ``__call__`` and a ``partial`` around one are caught too.
        """
        result = self.build_ui()
        if not inspect.isawaitable(result):
            return
        # Close before reporting, or a "never awaited" warning follows the
        # error and reads as a second, separate fault. Two coroutines can
        # be in hand: the theme wrapper's, and the one it is holding for a
        # build_ui that returns rather than is one. The nested one is
        # collected by shape from the outer's frame rather than by the
        # name it happens to be bound to, so renaming a local in the
        # wrapper cannot quietly stop closing it.
        pending = [result]
        frame = getattr(result, "cr_frame", None)
        if frame is not None:
            pending.extend(v for v in frame.f_locals.values() if inspect.isawaitable(v))
        for item in pending:
            close = getattr(item, "close", None)
            if callable(close):
                close()
        raise TypeError(
            f"{type(self).__name__} composes its component tree in __init__, which "
            f"cannot await, so build_ui must be synchronous here; it returned "
            f"{type(result).__name__}."
            f"\n  Fix: keep build_ui a plain def and move the async work into "
            f"on_load(), which the library awaits before every render."
        )

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
                # Only Buttons and Selects carry a real custom_id. Every
                # discord.ui.Item sets _provided_custom_id in its own
                # __init__, so a hasattr gate here would also rewrite
                # TextDisplay, Container, Separator, ActionRow, and
                # Section: inert on the wire, but the stray attribute
                # then reads as an unstable id to any later tree walk.
                if not isinstance(item, (Button, BaseSelect)):
                    continue
                if item._provided_custom_id:
                    continue
                # Link and premium buttons carry a url / sku_id and never a
                # custom_id; Discord rejects a button that has a custom_id
                # alongside either, so neither kind is stabilized.
                if (
                    getattr(item, "url", None) is not None
                    or getattr(item, "sku_id", None) is not None
                ):
                    continue
                callback = getattr(item, "original_callback", None)
                # A partial or any other callable object carries no
                # __qualname__, and this runs inside every build_ui.
                callback_name = getattr(callback, "__qualname__", None) or "none"
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
                item.custom_id = self._fit_custom_id(f"{prefix}:{ck}")
            else:
                item.custom_id = self._fit_custom_id(f"{prefix}:{ck}@{c_idx}.{p_idx}")
            # Assigning custom_id flips discord.py's own _provided_custom_id
            # to True, and the new id no longer matches the auto-generated
            # hex pattern, so both signals a persistent view uses to detect
            # a missing custom_id are gone by the time it validates. Mark the
            # rewrite so _validate_custom_ids can still tell the difference.
            # These ids anchor on ``self.id``, which is per-instance, so they
            # are stable across rebuilds but NOT across a restart.
            item._cascadeui_stabilized = True
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
        ``disabled``, ``url``, ``placeholder``, emoji string form,
        (for ``TextDisplay``/``Container`` items) the visible text,
        (for ``MediaGallery``/``Thumbnail``/``File`` items) the media
        URL plus its description/spoiler flags, (for ``Separator``)
        spacing and visibility, and (for buttons and selects) the
        cardinality, the premium ``sku_id``, the full option state, an
        entity select's ``default_values``, and a ``ChannelSelect``'s
        ``channel_types``.
        Anything else -- internal python ids, callback identity, ephemeral
        view back-references -- is deliberately excluded. Two views that
        would render identical bytes on the wire must produce the same
        digest, and two views that differ in any user-visible way must
        not. ``TestRenderDigestWireCoverage`` holds that second promise to
        the serializer: it derives each type's field set from
        ``to_component_dict`` rather than from this list, so a field
        discord.py adds later fails there instead of silently skipping a
        render.

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
            # ``id`` is wire-visible on every component type, so it rides the
            # walk rather than each per-type branch. Without it a rebuild that
            # renumbers nodes and changes nothing else hashes identically and
            # refresh() skips the edit, leaving Discord holding the old ids.
            component_id = getattr(item, "id", None)
            if component_id is not None:
                parts.append(("i", component_id))
            # Every display branch below is typed, and the ``custom_id``
            # test that follows them is the untyped catch-all for Buttons
            # and Selects. Ordering the typed branches first keeps that
            # catch-all from claiming an item it does not describe: which
            # classes carry a ``custom_id`` is discord.py's to change, and
            # a display item that gained one would otherwise be hashed as a
            # button and lose the fields that actually render.
            if isinstance(item, TextDisplay):
                parts.append(("t", item.content))
            # Container accents are wire-visible: a theme change that only
            # recolors marked cards must produce a new digest so the
            # re-render ships.
            elif isinstance(item, Container):
                # discord.py types this Optional[Union[Colour, int]] and
                # stores an int verbatim, so both spellings arrive here from
                # a Container the caller built without a builder. Hashing
                # the int form of each keeps one colour to one digest.
                accent = item.accent_color
                if isinstance(accent, discord.Colour):
                    accent = accent.value
                parts.append(("c", accent, item.spoiler))
            # Media-carrying items: the URL is the wire-visible state. A
            # rebuild that swaps only a banner or avatar URL must change
            # the digest, or refresh() short-circuits and the stale image
            # stays on screen.
            elif isinstance(item, MediaGallery):
                parts.append(
                    ("g", tuple((g.media.url, g.description, g.spoiler) for g in item.items))
                )
            elif isinstance(item, Thumbnail):
                parts.append(("th", item.media.url, item.description, item.spoiler))
            elif isinstance(item, UIFile):
                parts.append(("f", item.media.url, item.spoiler))
            # A Separator ships ``spacing`` and ``visible`` (the latter as the
            # ``divider`` key), so a rule that appears, disappears, or changes
            # height is a user-visible change. Without this branch an id-less
            # Separator contributed nothing at all and removing one left the
            # digest byte-identical.
            elif isinstance(item, Separator):
                parts.append(("s", getattr(item.spacing, "value", item.spacing), item.visible))
            # Buttons and selects: record the wire-visible attributes.
            elif hasattr(item, "custom_id"):
                # A select's rendered selection lives in opt.default, which
                # none of the scalar attributes above capture. set_selected()
                # mutates exactly that field, so a selection-only rebuild
                # would otherwise hash identical and refresh() would drop the
                # re-render. Walk options (string selects only -- auto-populated
                # UserSelect/RoleSelect/etc. expose no .options, so the getattr
                # returns None for them and for buttons).
                options = getattr(item, "options", None)
                option_state = (
                    tuple(
                        (
                            opt.value,
                            opt.default,
                            opt.label,
                            opt.description,
                            str(opt.emoji) if opt.emoji else None,
                        )
                        for opt in options
                    )
                    if options
                    else None
                )
                # An entity select carries its selection in default_values
                # instead of an option list: the same state opt.default holds
                # for a string select, one select family over. Each entry is a
                # SelectDefaultValue whose id and type are what ship.
                defaults = getattr(item, "default_values", None)
                default_value_state = (
                    tuple((dv.id, getattr(dv.type, "value", dv.type)) for dv in defaults)
                    if defaults
                    else None
                )
                # ChannelSelect only; the filter decides which channels the
                # picker offers, so a rebuild that narrows it is visible.
                channel_types = getattr(item, "channel_types", None)
                channel_type_state = (
                    tuple(getattr(ct, "value", ct) for ct in channel_types)
                    if channel_types
                    else None
                )
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
                        # Cardinality is what makes a select clearable or
                        # multi-pick; a premium button ships a sku_id and no
                        # label. Both are None on the component families that
                        # do not carry them.
                        getattr(item, "min_values", None),
                        getattr(item, "max_values", None),
                        getattr(item, "sku_id", None),
                        option_state,
                        default_value_state,
                        channel_type_state,
                    )
                )
        return hash(tuple(parts))

    def _freeze_components(self) -> int:
        """Disable all interactive components in this view.

        V2 LayoutViews nest buttons inside ActionRow/Container, so the
        full tree is walked to reach them. V1 Views have flat children.

        Returns the count of components newly disabled. A component-less
        display view (a card of text and images) freezes nothing, so
        callers skip the cosmetic edit rather than ship a no-op PATCH that
        only re-sends an identical tree.
        """
        items = self.walk_children() if self._is_layout() else self.children
        frozen = 0
        for item in items:
            if hasattr(item, "disabled") and not item.disabled:
                item.disabled = True
                frozen += 1
        return frozen

    async def on_timeout(self) -> None:
        """Called when the view times out. Disables all components and cleans up state.

        Freezing is unconditional here: ``exit_policy`` governs close
        gestures, and an expiry is not one. Override this method to
        delete the message on timeout instead.
        """
        # Exit tracked child views first
        await self._cleanup_attached_children()

        # Tear down tasks and both registries BEFORE the cosmetic freeze edit,
        # mirroring exit(). _destroy_view frees the instance-limit slot up front
        # so a concurrent send() does not count this timed-out view during the
        # up-to-edit_timeout edit below, and removes the state entry before the
        # active entry so a failed dispatch cannot strand a ghost.
        self.task_manager.cancel_tasks(self.id)
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        await self.state_store._destroy_view(self.id, source_id=self.id)

        # Skip the edit only when it would ship what is already on screen: a
        # component-less display view (or one already fully disabled) would
        # otherwise send a no-op PATCH on every timeout. The tree is checked
        # alongside the freeze because an override that rebuilds before
        # delegating here has something to say even though nothing froze.
        if self._message and (
            self._freeze_components() or self._compute_tree_digest() != self._last_tree_digest
        ):
            try:
                await self._bounded(self._message.edit(**self._freeze_edit_kwargs()))
            except discord.NotFound:
                pass  # Message was already deleted
            except asyncio.TimeoutError as e:
                if isinstance(e, aiohttp.ClientError):
                    # aiohttp's connect and socket timeouts inherit both, and
                    # fire below edit_timeout, so naming a stall here would
                    # point a reader at the ceiling rather than the network.
                    logger.warning(
                        f"Disabling components on timeout did not reach Discord "
                        f"for {type(self).__name__}: {describe_discord_error(e)}. "
                        f"View torn down regardless."
                    )
                else:
                    logger.warning(
                        f"Timed out disabling components on timeout for "
                        f"{type(self).__name__}; view torn down regardless."
                    )
            except Exception as e:
                # An ephemeral view that outlived its 15-minute interaction
                # token cannot be edited -- the 401 is expected and
                # unavoidable, not an error, so it logs at DEBUG. The view is
                # already dead; there is nothing left to disable. A
                # non-ephemeral edit failure is genuinely unexpected and
                # stays at WARNING.
                if self._ephemeral:
                    logger.debug(
                        f"Skipped disabling components on timeout for "
                        f"{type(self).__name__}: {e}. The interaction token "
                        f"likely expired (15-minute limit); ephemeral messages "
                        f"cannot be edited afterward."
                    )
                else:
                    logger.warning(f"Could not disable components on timeout: {e}.")

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

    async def on_message_gone(self) -> None:
        """Called when an edit observes the view's message is already gone.

        ``refresh()`` issues the edit that fails with ``discord.NotFound``
        when the message was deleted out from under the view (admin delete,
        bulk purge, channel delete). The library nulls ``self._message`` and
        calls this hook so a consumer that records the message elsewhere (its
        own database row, an external index) can reconcile that reference.
        Key the reconciliation on the view's stable identity, such as
        ``persistence_key``, since ``self._message`` is already nulled.

        Default is a no-op. This is the edit-path counterpart to two existing
        deletion signals: ``on_message_delete`` fires from the gateway
        ``MESSAGE_DELETE`` event for a message removed while the bot runs, and
        the ``REGISTRY_PRUNED`` action fires when reattach finds a persistent
        view's message gone after a restart. Unlike ``on_message_delete``,
        this hook does not exit the view: the gateway event owns teardown, and
        decoupling the signal keeps it safe to fire from the reactive refresh
        path. Make the reconciliation idempotent, since this hook and
        ``on_message_delete`` can both fire for the same deletion. The hook
        may run while the view's update lock is held, so an override may
        safely do I/O (reconcile a database row, call an external service)
        but should not dispatch a state change back into this view, which
        would re-enter the lock.
        """
        return

    async def _handle_state_notification(self, state, action):
        """React to state changes with update coalescing.

        When multiple dispatches trigger this callback concurrently on the
        same view (e.g. two players clicking buttons at once in a shared
        game), the second notification sets a pending flag and returns
        immediately. The first notification re-runs ``on_state_changed``
        with the latest store state after completing, capturing both
        changes in a single rebuild + edit cycle.

        Notifications that reach a finished view are dropped outright. The
        cross-view fan-out is fire-and-forget, so a task created ahead of a
        teardown can run after ``exit()`` completes; rebuilding then renders
        into a view that is no longer interactive, and reads attributes the
        teardown already cleared (an exited child's parent link, for
        example).

        Once the ephemeral refresh button has been armed, subsequent
        notifications are dropped: the view is intentionally frozen on the
        refresh button so it stays clickable inside the 90-second
        pre-warning window. Allowing rebuilds to proceed would clobber the
        button and leave the user with no recovery path once the
        interaction token expires.
        """
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")

        if self.is_finished():
            return

        if self._refresh_armed:
            return

        if self._update_lock.locked():
            self._update_pending = True
            return

        async with self._update_lock:
            while True:
                self._update_pending = False
                await self._run_state_changed(self.state_store.state)
                if not self._update_pending:
                    break

    async def on_pre_send(self, interaction: Optional[Interaction]) -> bool:
        """Pre-send veto hook -- gate the send before any work happens.

        Override to run a permission or data check (a database lookup, a
        cooldown, an entitlement gate) and decide whether the view should be
        sent at all. Return ``True`` to proceed (the default) or ``False`` to
        abort. An abort is clean: no Discord message ships and no state is
        registered, so a vetoed send leaves zero side effects.

        It runs first in the send pipeline -- before :meth:`on_load`,
        placement validation, instance enforcement, and the Discord call --
        so a veto skips all of that cost. The interaction's response slot is
        still open here, so an override can :meth:`respond` to explain the
        veto, and, when the send proceeds, the slot stays available for the
        actual delivery (no forced ``defer``).

        The slot-open guarantee assumes the hook completes within the ack
        budget. When ``send()`` runs inside a component callback and the hook
        itself takes longer than ``auto_defer_delay``, the auto-defer timer
        acks first and the send falls back to a followup -- still correct,
        just one round-trip slower. A fast check (a cache read, an
        in-memory lookup) preserves the single-round-trip path.

        ``interaction`` is the interaction that triggered the send, or
        ``None`` for a channel or context send (a prefix command). An
        override that needs the interaction guards for ``None``.
        """
        return True

    # Subclass config: the rebuild this view supplies for its own navigation
    # edits, used whenever the caller passes no rebuild= of its own.
    #
    # It exists because the two component versions disagree about what an
    # edit needs. A V2 view IS its component tree, so swapping view= is the
    # whole render and the default None is correct. A V1 view's content lives
    # in an embed the edit must carry, and pop() has no rebuild to pass (the
    # back button is library code with nothing to hand it). So a V1 view that
    # renders an embed names its own::
    #
    #     class Hub(StatefulView):
    #         nav_rebuild = staticmethod(lambda v: {"embed": v.build_embed()})
    #
    # A callable taking the destination view; a returned dict splats into the
    # edit. Wrap it in staticmethod() -- a bare lambda on a class body would
    # bind as a method and receive self. An explicit rebuild= always wins,
    # matching the class-attribute-then-argument precedence the policy
    # attributes use.
    nav_rebuild: ClassVar[Optional[Callable]] = None

    def get_nav_state(self) -> dict:
        """Return the view state that should survive a :meth:`pop`.

        ``pop`` does not restore the parent object -- it reconstructs one
        from the keyword arguments captured at construction and re-runs
        :meth:`on_load`. Data is therefore fresh, but anything the view
        selected *since* construction is not a constructor kwarg and reverts
        to its default: the page a user paged to, the tab they opened, the
        tier they picked. The failure is quiet and lands far from its cause,
        because the rebuilt view renders its defaults without complaint.

        Override to name what should carry across. ``push`` captures this on
        the view being pushed away from, and the matching :meth:`pop` hands
        it back to :meth:`restore_nav_state` on the reconstruction, before
        ``on_load`` runs::

            def get_nav_state(self):
                return {"severity": self._severity}

            def restore_nav_state(self, state):
                self._severity = state.get("severity", self._severity)

        The returned mapping is held on the navigation stack and never
        serialized, so it may carry live objects. It lives as long as the
        stack entry does.

        This is for view-local selection state. Data genuinely shared
        *between* a parent and its child belongs in ``shared_data`` (via
        ``update_session``), which lives on the session and already outlives
        both views.

        Default returns an empty mapping, so a view that needs none of this
        pays nothing.
        """
        return {}

    def restore_nav_state(self, state: dict) -> None:
        """Reapply the state captured by :meth:`get_nav_state`.

        Called on a view reconstructed by :meth:`pop`, after ``__init__`` and
        before :meth:`on_load`, so a preload reads the restored selection
        rather than the constructor's default. Receives whatever
        ``get_nav_state`` returned at push time (an empty mapping when the
        view did not override it).

        Read defensively -- treat every key as optional. The stack entry was
        written by an earlier version of this view, and a key the class no
        longer sets is the caller's to tolerate.

        Args:
            state: The mapping captured at push time.
        """
        return

    def _capture_nav_state(self) -> dict:
        """Run ``get_nav_state``, degrading to an empty mapping on failure.

        A broken override costs the restore, never the navigation: the user
        still reaches the view they clicked toward, and lands on the
        constructor's defaults rather than on an error.
        """
        try:
            state = self.get_nav_state()
        except Exception as exc:
            logger.warning(f"get_nav_state raised in {type(self).__name__}: {exc}")
            return {}
        if state is None:
            return {}
        if not isinstance(state, dict):
            logger.warning(
                f"get_nav_state in {type(self).__name__} returned "
                f"{type(state).__name__}, expected dict; nav state discarded"
            )
            return {}
        return state

    def _apply_nav_state(self, state: dict) -> None:
        """Run ``restore_nav_state``, swallowing a raising override.

        Same containment as :meth:`_capture_nav_state`: a failed restore
        leaves the view on its defaults instead of stranding the user
        mid-navigation with no way back.
        """
        if not state:
            return
        try:
            self.restore_nav_state(state)
        except Exception as exc:
            logger.warning(f"restore_nav_state raised in {type(self).__name__}: {exc}")

    async def on_load(self) -> None:
        """Async data preload and tree rebuild before the view is displayed.

        Override to fetch from a database, an API, or any other async
        source and build the component tree against the result. The
        library calls it automatically at three seams so a view never
        renders against stale or placeholder data:

        - :meth:`send` -- before the initial Discord message ships (and
          before placement validation), so the first render reflects
          loaded data.
        - :meth:`push` / :meth:`pop` -- on the destination view before the
          navigation edit, so navigating to a child or back to a parent
          re-reads its source (the reload-on-render pattern). Defining
          ``on_load`` supersedes passing ``rebuild=lambda v:
          v.load_and_build()`` -- the navigation calls run it automatically.
        - :meth:`reload` -- the out-of-band convenience (``on_load`` then
          ``refresh``) for re-fetching from inside a callback.

        The synchronous ``__init__`` builds against a placeholder (an
        empty list, a "Loading..." card) so construction stays
        inspectable; ``on_load`` is where the only genuinely async work
        happens. Default is a no-op, so views without async preload pay
        nothing.
        """
        return

    async def _run_on_load(self) -> None:
        """Run ``on_load()``, warning once per class if it overruns the budget.

        ``on_load`` runs on the render path -- the initial send, every push/pop
        navigation, and ``reload()``. A preload slower than ``auto_defer_delay``
        delays navigation and competes with the interaction ack, so a slow
        ``on_load`` is the usual silent cause of a sluggish panel. The warning
        surfaces it at the seam; behavior is unchanged (``on_load`` still runs
        identically, and the timing wrapper is skipped entirely for the no-op
        default).
        """
        if type(self).on_load is _StatefulMixin.on_load:
            return  # Default no-op -- skip the timing wrapper.
        start = time.monotonic()
        await await_maybe(self.on_load())
        elapsed = time.monotonic() - start
        if elapsed > self.auto_defer_delay:
            cls_name = type(self).__name__
            if cls_name not in _slow_on_load_warned:
                _slow_on_load_warned.add(cls_name)
                logger.warning(
                    f"{cls_name}.on_load() took {elapsed:.1f}s, over auto_defer_delay "
                    f"({self.auto_defer_delay}s). Slow preloads on the render path delay "
                    f"navigation and compete with interaction acks. Keep HTTP off the "
                    f"render path -- resolve from cache or batch fetches into one round-trip."
                )

    async def _run_state_changed(self, state) -> None:
        """Run ``on_state_changed()``, warning once per class if it overruns budget.

        A reactive rebuild seam (a live board rebuilding on a state change)
        competes with the interaction ack the same way ``on_load`` does, but its
        duration is otherwise only sampled under DevTools perf profiling, so a
        production board renders through no surfaced timing. This warns at the
        always-on seam when an OVERRIDE overruns ``auto_defer_delay``. The
        default ``on_state_changed`` (build_ui + refresh) is not timed here --
        its cost is the user's build_ui plus the message edit, neither of which
        this coarse budget cleanly isolates.

        The budget is an acknowledgement deadline, so the timing runs only while
        an interaction is in flight. A rebuild driven by a timeout or an
        out-of-band dispatch races no ack, and its elapsed time is dominated by
        the message edit rather than by the rebuild.
        """
        if (
            type(self).on_state_changed is _StatefulMixin.on_state_changed
            or _CURRENT_INTERACTION.get() is None
        ):
            await await_maybe(self.on_state_changed(state))
            return
        start = time.monotonic()
        await await_maybe(self.on_state_changed(state))
        elapsed = time.monotonic() - start
        if elapsed > self.auto_defer_delay:
            cls_name = type(self).__name__
            if cls_name not in _slow_render_warned:
                _slow_render_warned.add(cls_name)
                logger.warning(
                    f"{cls_name}.on_state_changed() took {elapsed:.1f}s, over "
                    f"auto_defer_delay ({self.auto_defer_delay}s). A slow reactive "
                    f"rebuild competes with interaction acks. Keep HTTP off the "
                    f"render path -- resolve from cache or batch fetches."
                )

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

        A view with no ``build_ui`` still refreshes. Its tree is composed
        elsewhere (in ``__init__``, or by a rebuild method such as a tab
        switch or a wizard step advance), so the already-composed tree is
        what ships. This is what a throttled refresh depends on: the
        deferred edit re-enters this method at the cooldown boundary, and
        returning without an edit would drop it. The render-hash
        short-circuit in :meth:`refresh` makes the call free when the tree
        is unchanged.

        Args:
            state: The current application state.
        """
        build = getattr(self, "build_ui", None)
        kwargs = {}
        if build is not None:
            result = build()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                kwargs = result
        await self.refresh(**kwargs)

    async def reload(self, **kwargs) -> Optional["RenderOutcome"]:
        """Re-run :meth:`on_load`, then edit the message to show the result.

        The out-of-band counterpart to the automatic ``on_load`` calls on
        send/push/pop: use it inside a callback that mutates the view's
        data source and needs the display to re-fetch and re-render
        immediately (a create/edit/archive action, a manual refresh
        button). Equivalent to ``await self.on_load()`` followed by
        ``await self.refresh()``.

        Reloads on one view run one at a time. Interactions serialize on
        their own lock and state notifications coalesce, but a reload can
        arrive from a background task while another is mid-fetch, and two
        ``on_load`` bodies interleaving on the same view read and stamp
        each other's half-built state. A reload that finds one in flight
        waits its turn, then runs its own fetch against the source as it
        stands at run time, so the last reload to run renders the freshest
        data. Calling ``reload()`` from inside the view's own ``on_load``
        or render path raises ``RuntimeError``: the surrounding reload
        already re-runs the fetch and ships the result.

        Returns a :class:`RenderOutcome` naming what happened (rendered,
        skipped as unchanged, deferred to the throttle boundary, dropped in
        transit, or no message to edit), or ``None`` when a subclass
        render override reports nothing. Callers that must know the edit
        landed (a one-shot notice, a stamp written after the render) check
        the outcome instead of assuming a returned ``reload()`` rendered.

        Respects the refresh throttle at the reload layer: a reload landing
        inside an active cooldown (``refresh_cooldown_ms`` or a 429 backoff)
        defers the whole reload (``on_load``'s fetch included), returns
        ``RenderOutcome.DEFERRED``, and a burst collapses to one fetch +
        edit at the window boundary. The deferred boundary task replays the
        coalesced calls' keyword arguments, so a subclass that adds a reload
        keyword (e.g. ``force``) must forward it to ``super().reload(**kwargs)``
        for the replay to carry it. Boolean keywords OR across coalesced
        calls (a ``force=True`` is never dropped by a later unforced reload
        in the same window); any other keyword takes the newest call's
        value. A keyword consumed before ``super().reload()`` whose effect
        is not already in pre-gate view state is otherwise dropped when the
        reload defers.
        """
        if self._reload_task is not None and self._reload_task is asyncio.current_task():
            raise RuntimeError(
                f"reload() called from inside its own on_load() or render path on "
                f"{type(self).__name__}. The surrounding reload() already re-runs "
                f"on_load and ships the result. Fix: mutate state and return, or "
                f"schedule the follow-up reload with create_task()."
            )
        async with self._reload_lock:
            self._reload_task = asyncio.current_task()
            try:
                # Cleared at run start, inside the lock, so a queued reload
                # clears its own run's flag rather than one a concurrent
                # holder is mid-render on. reload() can still return at the
                # throttle gate below without reaching refresh(), so the
                # clear stays ahead of the gate rather than describing
                # whatever the previous call did.
                self._refresh_degraded = False
                # reload() never takes the acting waiver. Its gate throttles
                # the on_load fetch, not just the edit, so waiving it for
                # interaction-driven reloads would turn a manual refresh
                # button into an unbounded query against the caller's data
                # source.
                if self._refresh_armed:
                    # Same reason the deferred path and the notification
                    # dispatcher refuse to rebuild here: the tree is the
                    # refresh button now, and re-running on_load would
                    # replace it. The armed flag then drops every
                    # notification that could put it back, leaving a stale
                    # panel with no recovery once the webhook token expires.
                    return await self.refresh()

                now = time.monotonic()
                wait = self._throttle_until() - now
                if wait > 0:
                    if self._reload_pending:
                        self._pending_reload_kwargs = _merge_reload_kwargs(
                            self._pending_reload_kwargs, kwargs
                        )
                    else:
                        self._reload_pending = True
                        self._pending_reload_kwargs = dict(kwargs)
                    self._queue_deferred_refresh(wait)
                    return RenderOutcome.DEFERRED
                await self._run_on_load()
                return await self._reload_render()
            finally:
                self._reload_task = None

    async def _reload_render(self) -> Optional["RenderOutcome"]:
        """Render step of :meth:`reload`, after ``on_load`` runs.

        The base ships a bare ``refresh()``, which is correct for V2 views
        whose ``on_load`` rebuilds the component tree that ``refresh()`` then
        ships. A V1 pattern whose content is an embed overrides this to route
        through its embed-carrying render, so ``reload()`` updates the embed
        instead of shipping an edit with no kwargs.

        Returns the :class:`RenderOutcome` of the edit it shipped, so
        ``reload()`` can relay it. An override returns the outcome of its
        final ``refresh(...)`` call; returning ``None`` makes ``reload()``
        report nothing for the run.
        """
        return await self.refresh()

    def _note_transport_failure(self, error: BaseException, *, where: str) -> None:
        """Record an edit that never reached Discord.

        Clears the render baseline so the next refresh ships unconditionally,
        and raises the flag :attr:`refresh_degraded` reports. Shared by every
        seam that can see one, including the timeout handlers: aiohttp's
        connect and socket timeouts inherit BOTH ``ClientError`` and
        ``asyncio.TimeoutError``, so a timeout clause placed first sees them
        before the transport clause ever runs. A connect that never completed
        is not the indeterminate case a cancelled ``wait_for`` is -- the
        request definitively never left the host.
        """
        logger.warning(
            f"{where} did not reach Discord for {type(self).__name__}: "
            f"{describe_discord_error(error)}. The next refresh re-ships."
        )
        self._last_tree_digest = None
        self._refresh_degraded = True

    @contextlib.contextmanager
    def restore_on_dropped_render(self, *attributes: str, rebuild: Optional[Callable] = None):
        """Undo view-local writes when the render that would show them is dropped.

        A callback that changes the view and then renders has two states to
        keep together: the attribute and the screen. ``refresh()`` swallows a
        transport failure, so the attribute can move while the screen does
        not, and the next click reads a view the user never saw.

        The sharp case is a control armed on one press and executed on the
        next. If the arming render never reaches Discord, the button on
        screen still looks unarmed, so the obvious response is to press it
        again -- and that press executes, because the flag is already set. A
        dropped packet turns a two-press confirmation into one, on exactly
        the controls that ask for confirmation because they are destructive.

        Snapshots each named attribute on entry and rebinds it if the render
        was dropped. Pass ``rebuild`` to recompose the tree from the restored
        values, which a V2 view needs and a V1 view carrying its body on an
        ``embed`` kwarg does not::

            with self.restore_on_dropped_render("_confirming", rebuild=self.build_ui):
                self._confirming = True
                self.build_ui()
                await self.refresh()

        Without it the attribute goes back and the tree does not, so the two
        describe different states: a V2 tree IS the content, so the next
        ``refresh()`` that does not rebuild first ships a screen the restored
        attribute no longer names. ``rebuild`` runs only on a drop, after the
        attributes are back, and is not re-rendered -- the render that failed
        is the one being undone, and the next one ships the corrected tree.

        The snapshot holds each attribute's value, so a name rebound inside
        the block comes back and a list or dict mutated in place does not.
        Flags and cursors are what this is for; rebuild a collection from the
        restored cursor rather than editing it under the manager.

        Nothing is restored when the render lands, nor when the block raises
        -- an exception is its own signal and the caller owns the recovery.
        Entry clears :attr:`refresh_degraded`, so the answer on exit describes
        a render this block made rather than one already dropped before it.

        Binding matters when two views are involved: the manager reads the
        flag of the view it is called on, so a caller deciding on one view
        while a sibling's render is the one that matters opens the block on
        whichever view performs the refresh.
        """
        if rebuild is not None:
            if not callable(rebuild):
                raise TypeError(
                    f"restore_on_dropped_render rebuild must be callable; "
                    f"got {type(rebuild).__name__}. Fix: pass the bound method itself, "
                    f"as in rebuild=self.build_ui."
                )
            if is_async_callable(rebuild):
                # The restore runs at the exit of a synchronous context
                # manager, which has no way to await. Rejecting here names the
                # problem at the call; returning an un-awaited coroutine would
                # leave the tree unrebuilt and say nothing.
                raise TypeError(
                    f"restore_on_dropped_render rebuild must be synchronous; "
                    f"{getattr(rebuild, '__qualname__', rebuild)} is async. Fix: rebuild "
                    f"after the block instead, guarded on `if self.refresh_degraded:`."
                )
        snapshot = {name: getattr(self, name) for name in attributes}
        # The flag is sticky until the next refresh clears it, so a drop from
        # an earlier block (or from a refresh before this one) would answer for
        # a render the body never made, and revert a write that was fine. Seen
        # with a body whose refresh sits behind a conditional that did not run.
        self._refresh_degraded = False
        yield
        if not self.refresh_degraded:
            return
        for name, value in snapshot.items():
            setattr(self, name, value)
        if rebuild is not None:
            result = rebuild()
            if inspect.isawaitable(result):
                # An instance whose __call__ is async clears the check above.
                # Close it so it does not also warn about never being awaited,
                # then say what happened: the attributes are back and the tree
                # is not, which is the state the rebuild existed to prevent.
                result.close()
                raise TypeError(
                    f"restore_on_dropped_render rebuild returned an awaitable; the restore "
                    f"cannot await it, so the tree still renders the value that was rolled "
                    f"back. Fix: pass a synchronous rebuild, or rebuild after the block "
                    f"guarded on `if self.refresh_degraded:`."
                )
        if attributes:
            logger.debug(
                f"Render dropped in {type(self).__name__}; restored "
                f"{', '.join(attributes)} to match the screen."
            )

    def _edit_never_landed(self, previous: Any, current: Any, *, cursor: str) -> bool:
        """Whether a navigation cursor should rewind after a dropped edit.

        Every paging pattern moves its cursor before the repaint -- the page,
        the wizard step, the active tab. A transport failure is the one edit
        outcome where nothing shipped and nothing is known to be wrong, so it
        leaves the cursor pointing somewhere the screen never went, and the
        next click navigates from a position the user never saw.

        Answers the question only; the caller owns the restore, because what
        has to be rebuilt afterwards differs per pattern and per component
        version. ``previous`` of ``None`` means the caller moved no cursor
        (a data rebuild rather than a navigation), so nothing rewinds.
        """
        if previous is None or previous == current or not self.refresh_degraded:
            return False
        logger.debug(
            f"{cursor} change in {type(self).__name__} did not reach Discord; "
            f"rewinding from {current} to {previous} to match the screen."
        )
        return True

    @property
    def refresh_degraded(self) -> bool:
        """Whether the last :meth:`refresh` was dropped by a transport failure.

        A request that never reached Discord raises from aiohttp rather than
        discord.py, and ``refresh()`` swallows it: the tree is unchanged on
        screen, the state it renders is already committed, and the next state
        change re-ships it. Raising instead would put a failure card in front
        of the user over a repaint that merely needs repeating.

        Read this when the caller changed something *before* the render that
        should not stand if the render never landed. A paginated view is the
        worked example. The page cursor advances first, so a dropped edit
        otherwise leaves the reader on the previous page with the cursor
        already moved::

            before = self.current_page
            await self.refresh()
            if self.refresh_degraded:
                self.current_page = before

        Restoring the cursor is the whole rollback only when the rendered
        body rides an ``embed`` / ``content`` kwarg. A V2 tree IS the
        content, so a pattern that already recomposed its tree for the new
        cursor has to recompose it back as well, or the next refresh ships
        the state the cursor no longer names.
        ``_BasePaginatedMixin._update_page`` is the worked reference for
        both shapes.

        Reset at the top of every ``refresh()``, so it always describes the
        most recent call.
        """
        return self._refresh_degraded

    async def refresh(self, **kwargs) -> "RenderOutcome":
        """Edit the view's message to reflect the current component state.

        Passes ``view=self`` along with any extra *kwargs* (``embed``,
        ``content``, etc.) to ``message.edit()``.  Silently handles the
        case where the message no longer exists (``discord.NotFound``).

        Returns a :class:`RenderOutcome` naming what happened to the edit:
        ``RENDERED`` (shipped), ``SKIPPED`` (render-hash match, nothing
        owed), ``DEFERRED`` (a throttle or rate-limit window holds it and a
        scheduled task re-renders at the boundary), ``DROPPED`` (attempted
        and not known to have landed, nothing scheduled), or ``NO_MESSAGE``
        (no editable message remains). Callers that only repaint can ignore
        it; callers that must know the edit landed read it instead of
        inferring from a normal return.

        Also swallows a transport failure -- a request that never reached
        Discord, which raises from aiohttp and carries no HTTP status. The
        edit is dropped, the render baseline is cleared so the next state
        change re-ships, and :attr:`refresh_degraded` reports it. Raising
        instead would put ``on_error``'s failure card in front of the user
        over a repaint that merely needs repeating. Read
        :attr:`refresh_degraded` when the caller changed something before
        the render that should not stand if the render never landed.

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
        # Ahead of the no-message return, so a bad kwarg raises the same way
        # whether or not the view has been sent yet. Leaving it below would
        # reintroduce, in miniature, the send-state-dependent behavior this
        # guard exists to remove.
        self._reject_non_portable_edit_kwargs(kwargs)
        self._refresh_degraded = False

        if not self._message:
            return RenderOutcome.NO_MESSAGE

        # Is this edit the direct answer to a click on this view's own
        # message? Resolved before the gate, because the answer decides
        # which windows apply. Deliberately broader than the fast-path test
        # below: an already-acked slot (the auto-defer timer beat the
        # callback) still means a user is waiting on this edit, and only
        # changes which endpoint ships it.
        interaction = _CURRENT_INTERACTION.get()
        acting = (
            interaction is not None
            and interaction.type == discord.InteractionType.component
            and interaction.message is not None
            and interaction.message.id == self._message.id
        )

        # Throttle gate. Checked before the digest + edit path so a view
        # in cooldown costs one clock read, not a digest hash + REST call.
        # An acting edit waives the cooldown but not the rate-limit window
        # -- a page turn should not queue behind background pacing, but no
        # edit outruns a 429.
        now = time.monotonic()
        wait = self._throttle_until(acting=acting) - now
        if wait > 0:
            self._queue_deferred_refresh(wait)
            return RenderOutcome.DEFERRED

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

            # Theme-managed accents resolve against the live view theme
            # before the digest, so a runtime theme change alters the
            # digest and ships as a re-render instead of being skipped.
            self._apply_theme_defaults()
            self._sync_back_buttons()

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
                    return RenderOutcome.SKIPPED

            # Pre-flight check on the assembled tree before any of the
            # three edit paths ships. Skipped refreshes (digest match
            # above) bypass this -- nothing changed, the previous send
            # already validated. Catches mid-session shape changes
            # (Wizard step swaps, Form section toggles, Tab body
            # rebuilds) that would otherwise surface as HTTP 400 from
            # Discord rather than a clear ``ValueError`` at the seam.
            self._check_placement()

            # Mention rules ride every edit path explicitly. discord.py
            # applies the client-level default on the interaction and
            # webhook endpoints, but ``Message.edit`` only forwards it
            # when ``content`` is supplied, and a V2 view never supplies
            # content, since the component tree is the content. Resolving
            # here keeps all three paths shipping the same payload no
            # matter which one the runtime picks. Injected after the digest
            # short-circuit above, which is gated on an empty kwargs dict.
            mentions = self._resolve_allowed_mentions(kwargs.pop("allowed_mentions", None))
            if mentions is not None:
                kwargs["allowed_mentions"] = mentions

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
            # Ephemeral acting views take the ack-first branch instead: a
            # webhook-only edit runs too slowly to double as the ack without
            # risking the 3s deadline.
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
            # ``acting`` is resolved above the throttle gate. The fast path
            # needs one condition beyond it: the response slot must still be
            # open, since the edit rides the ack packet.
            slot_open = acting and not interaction.response.is_done()
            if slot_open and not self._ephemeral:
                fast_path_timeout = max(0.5, self.auto_defer_delay - 1.0)
                try:
                    await asyncio.wait_for(
                        interaction.response.edit_message(view=self, **kwargs),
                        timeout=fast_path_timeout,
                    )
                    self._last_tree_digest = self._compute_tree_digest()
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown(acting=acting)
                    return RenderOutcome.RENDERED
                except asyncio.TimeoutError as e:
                    if isinstance(e, aiohttp.ClientError):
                        self._note_transport_failure(e, where="Refresh")
                        return RenderOutcome.DROPPED
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
                    return RenderOutcome.DROPPED
                except discord.InteractionResponded:
                    # The auto-defer timer acked in the window between the
                    # is_done() guard and edit_message's own internal guard.
                    # InteractionResponded is a sibling of HTTPException (not a
                    # subclass), so it would otherwise escape the handler below.
                    # The guard raises before any HTTP, so no edit shipped and
                    # the interaction is already acked -- fall through to the
                    # channel path to ship the edit (no ack-budget concern,
                    # unlike the cancelled-fast-path TimeoutError case above).
                    pass
                except DISCORD_CALL_ERRORS as e:
                    if self._handle_rate_limit(e):
                        return RenderOutcome.DEFERRED
                    # Any other HTTP error falls through to the channel path
                    # so a transient failure on the interaction endpoint does
                    # not lose the edit entirely.
            elif acting and self._ephemeral:
                # Ephemeral acting refreshes are NOT pre-deferred here. The edit
                # below ships through the webhook handle (self._message.edit()),
                # which rides the original send's interaction token -- independent
                # of this click's ack -- so it lands without first waiting on a
                # deferred-update round-trip. The click is acknowledged after the
                # callback by the post-callback defer in _scheduled_task, or at
                # auto_defer_delay by the auto-defer timer (which runs outside the
                # interaction lock) when the edit is slow. The edit-as-ack fast
                # path stays gated to non-ephemeral views above, where the edit
                # is fast enough to double as the ack.
                pass

            # Interaction-response messages ignore embed edits via the channel
            # endpoint (PATCH /channels/{id}/messages/{id}).  When the caller
            # passes embed/embeds kwargs, try the stored webhook message first
            # -- its .edit() routes through the interaction webhook which CAN
            # update embeds.  Falls back to the plain Message on token expiry.
            if ("embed" in kwargs or "embeds" in kwargs) and self._webhook_message:
                try:
                    await self._bounded(self._webhook_message.edit(view=self, **kwargs))
                    self._last_tree_digest = self._compute_tree_digest()
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown(acting=acting)
                    return RenderOutcome.RENDERED
                except asyncio.TimeoutError as e:
                    if isinstance(e, aiohttp.ClientError):
                        # Not token expiry, so the webhook handle survives.
                        self._note_transport_failure(e, where="Webhook edit")
                        return RenderOutcome.DROPPED
                    logger.warning(
                        f"Webhook edit stalled past {self.edit_timeout}s in "
                        f"{type(self).__name__}; the next refresh re-ships."
                    )
                    self._last_tree_digest = None
                    return RenderOutcome.DROPPED
                except DISCORD_CALL_ERRORS as e:
                    if self._handle_rate_limit(e):
                        return RenderOutcome.DEFERRED
                    if isinstance(e, aiohttp.ClientError):
                        # A dropped connection says nothing about the token, and
                        # this handle is the only endpoint that can edit an embed
                        # on an interaction-owned message. Falling through would
                        # forfeit it permanently over a blip and then silently
                        # drop every later embed edit through the channel path.
                        self._note_transport_failure(e, where="Webhook edit")
                        return RenderOutcome.DROPPED
                    # Token expired (15-min lifetime) -- fall through to channel endpoint
                    self._webhook_message = None

            try:
                await self._bounded(self._message.edit(view=self, **kwargs))
                self._last_tree_digest = self._compute_tree_digest()
                if perf_on:
                    store._record_edit()
                self._stamp_cooldown(acting=acting)
                return RenderOutcome.RENDERED
            except discord.NotFound:
                # The message was deleted out from under the view (admin
                # delete, purge, channel delete) and this edit just observed
                # the 404. Null the ref so the top-of-method guard short-
                # circuits later refreshes instead of re-issuing doomed edits,
                # then fire the reconcile hook so a consumer tracking this
                # message externally can clear it. The hook is guarded because
                # refresh() must stay non-raising -- the navigation fallbacks
                # rely on it absorbing NotFound.
                self._message = None
                try:
                    await await_maybe(self.on_message_gone())
                except Exception as exc:
                    logger.error(
                        f"on_message_gone failed for {type(self).__name__}: {exc}",
                        exc_info=exc,
                    )
                return RenderOutcome.NO_MESSAGE
            except asyncio.TimeoutError as e:
                if isinstance(e, aiohttp.ClientError):
                    self._note_transport_failure(e, where="Refresh")
                else:
                    logger.warning(
                        f"Edit stalled past {self.edit_timeout}s in "
                        f"{type(self).__name__}; the next refresh re-ships."
                    )
                    self._last_tree_digest = None
                return RenderOutcome.DROPPED
            except DISCORD_CALL_ERRORS as e:
                if self._ephemeral and getattr(e, "status", None) == 401:
                    # The lifecycle exit() and on_timeout() already classify:
                    # an ephemeral past the 15-minute webhook cliff has no
                    # token left, so no edit can land. Raising instead puts an
                    # ERROR and a traceback through the subscriber wrapper on
                    # every dispatch, for a condition the library treats as
                    # normal at every other edit seam.
                    logger.debug(
                        f"Refresh skipped: ephemeral webhook token expired "
                        f"for {type(self).__name__}."
                    )
                    return RenderOutcome.NO_MESSAGE
                elif isinstance(e, aiohttp.ClientError):
                    # The request never reached Discord, so nothing about the
                    # message is known to be wrong. The tree is already
                    # rebuilt and the state it renders is committed, and the
                    # next refresh re-ships it. Raising instead turns a
                    # dropped connection into on_error's failure card over a
                    # read-only repaint, which reads to the user as the
                    # content being untrustworthy rather than the network
                    # being briefly down.
                    self._note_transport_failure(e, where="Refresh")
                    return RenderOutcome.DROPPED
                elif not self._handle_rate_limit(e):
                    raise
                return RenderOutcome.DEFERRED
        finally:
            if perf_on:
                store._refresh_samples.append(
                    {
                        "view_id": self.id,
                        "view_class": type(self).__name__,
                        "refresh_ms": (time.perf_counter() - t0) * 1000,
                        "skipped": skipped,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )

    def _handle_rate_limit(self, error: BaseException) -> bool:
        """Detect a rate-limit and arm the reactive backoff window.

        Returns ``True`` when ``error`` is a rate-limit and the next-allowed
        timestamp has been stamped (caller should swallow the exception).
        Returns ``False`` for any other HTTP error (caller should re-raise
        or handle per its own contract).

        Three exception types reach here now that the shared catch-tuple
        covers transport failures, and only one of them carries the delay as
        an attribute. An ``aiohttp.ClientError`` is never a rate limit, so it
        falls straight through to ``False`` and its caller degrades.
        ``discord.RateLimited`` exposes ``retry_after`` directly. It appears only when the client sets
        ``max_ratelimit_timeout``, and from either side of the request: the
        bucket can predict the wait is too long and refuse to send, or a real
        429 can come back asking for longer than the ceiling allows. Either
        way the delay it carries is authoritative. It subclasses
        ``DiscordException`` rather than ``HTTPException``, so every seam
        that calls this names it explicitly in its ``except``.

        A 429 ``HTTPException`` carries no ``retry_after`` at all --
        discord.py's parser keeps only ``code`` and ``message`` from the
        body -- so the delay is read from the ``Retry-After`` response
        header instead.

        Reaching this path at all narrows what the 429 can be, and the
        answer is not "the view is busy" -- it is "the bot is banned".
        discord.py absorbs and retries every ordinary rate-limit itself, and
        raises only when the reply carries no ``Via`` header: its own test
        for a request Cloudflare blocked before Discord saw it. The window
        armed here therefore paces a bot that cannot reach Discord at all,
        not one that is merely being told to slow down. A header-less ban
        falls back to ``_CLOUDFLARE_BAN_BACKOFF``, which explains why that
        value is minutes-scale and why no interactive latency rides on it.
        """
        if isinstance(error, discord.RateLimited):
            self._ratelimit_not_before = time.monotonic() + error.retry_after
            self._schedule_backoff_retry()
            return True
        if getattr(error, "status", None) != 429:
            return False
        headers = getattr(getattr(error, "response", None), "headers", None) or {}
        try:
            retry = max(0.0, float(headers.get("Retry-After", _CLOUDFLARE_BAN_BACKOFF)))
        except (TypeError, ValueError):
            retry = _CLOUDFLARE_BAN_BACKOFF
        self._ratelimit_not_before = time.monotonic() + retry
        self._schedule_backoff_retry()
        return True

    def _schedule_backoff_retry(self) -> None:
        """Queue the edit this rate-limit just discarded to ship at the boundary.

        Every seam that catches a 429 stamps the window and returns, so the
        edit in flight is dropped. Most views live through that: their next
        state notification renders whatever is current and the loss is
        invisible. One view does not. ``_arm_refresh_button`` sets
        ``_refresh_armed`` *before* its edit, and that flag then drops every
        notification that could repair it -- so a 429 on the arming edit
        leaves a frozen panel with no refresh button and nothing left to give
        it one, which is exactly the outcome the 810s handoff exists to
        prevent.

        Shares the single ``_deferred_refresh_task`` slot with the cooldown
        path, so a burst of 429s queues one retry rather than one per failure,
        and re-entry from inside that task is allowed -- a retry that is
        rate-limited again schedules its own successor, so the view recovers
        whenever the block finally lifts instead of giving up after one try.
        Automatic retry is only defensible because the window is
        minutes-scale (see ``_CLOUDFLARE_BAN_BACKOFF``): at a one-second
        backoff this would be a bot hammering a ban that 429s help sustain.
        """
        if self.is_finished() or not self._message:
            return
        wait = self._ratelimit_not_before - time.monotonic()
        if wait > 0:
            self._queue_deferred_refresh(wait)

    def _queue_deferred_refresh(self, wait: float) -> None:
        """Own the single deferred-refresh slot.

        One task, whoever is asking -- the cooldown gate, the reload gate, or
        a rate-limit that just discarded an edit -- so a burst queues one
        retry rather than one per caller.

        The running task may replace itself. Without that, a deferred render
        blocked on re-entry finds itself registered as the pending retry,
        declines, and then clears the slot on its way out: the edit is lost
        with nothing left to ship it and nothing to report it. The ``finally``
        in :meth:`_deferred_refresh` only disowns the slot when it still
        points at the outgoing task, so a successor scheduled here survives.
        """
        current = self._deferred_refresh_task
        if current is None or current.done() or current is asyncio.current_task():
            self._deferred_refresh_task = self.create_task(self._deferred_refresh(wait))

    def _throttle_until(self, *, acting: bool = False) -> float:
        """The monotonic timestamp before which no edit should ship.

        Background edits answer to both windows. An *acting* edit -- one made
        in direct response to a user's click on this view's own message --
        waives the library's cooldown but never Discord's rate limit: a user
        who pressed a button is owed a response, while an endpoint returning
        429 is owed silence regardless of who asked.
        """
        if acting:
            return self._ratelimit_not_before
        return max(self._cooldown_not_before, self._ratelimit_not_before)

    async def _bounded(self, coro):
        """Await a Discord HTTP coroutine under the ``edit_timeout`` ceiling.

        ``asyncio.wait_for`` cancels the request when it stalls past
        ``edit_timeout`` seconds; aiohttp closes the connection on
        cancellation, so a hung socket cannot pin the awaiting code.
        ``edit_timeout = None`` awaits the coroutine directly with no
        ceiling. Raises ``asyncio.TimeoutError`` on stall so the caller
        can release the interaction lock and recover on the next edit.
        """
        if self.edit_timeout is None:
            return await coro
        return await asyncio.wait_for(coro, self.edit_timeout)

    async def _ack_bounded(self, coro):
        """Await an ack-coupled ``interaction.response.edit_message`` under the
        3s ack deadline.

        When the ack rides the same request as the edit, a stall must not pin
        the interaction past Discord's 3s deadline, so the bound is derived from
        ``auto_defer_delay`` (not ``edit_timeout``, which can be 60s). Raises
        ``asyncio.TimeoutError`` on stall so the caller falls through to the
        deferred path; the auto-defer timer (outside the lock) acks. Mirrors the
        bound the acting-view fast path in ``refresh()`` uses.
        """
        return await asyncio.wait_for(coro, timeout=max(0.5, self.auto_defer_delay - 1.0))

    # Edit kwargs every endpoint refresh() can route to will accept.
    # ``view`` is library-owned and never comes from a caller.
    _PORTABLE_EDIT_KWARGS = frozenset(
        {"allowed_mentions", "attachments", "content", "embed", "embeds"}
    )

    @classmethod
    def _reject_non_portable_edit_kwargs(cls, kwargs: dict) -> None:
        """Reject an edit kwarg only some of the three endpoints accept.

        ``refresh()`` picks its endpoint at runtime from conditions the
        caller cannot see: whether an interaction is bound, whether its
        response slot is open, whether the view is ephemeral. A kwarg the
        chosen endpoint does not take raises a ``TypeError`` from inside
        discord.py, so the same call site works for months and then fails
        when the ack race flips. Rejecting here makes the answer the same
        every time.

        Known asymmetries in discord.py 2.7: ``suppress_embeds`` is
        accepted only by ``InteractionResponse.edit_message``, and
        ``suppress`` only by ``Message.edit``.
        """
        stray = sorted(set(kwargs) - cls._PORTABLE_EDIT_KWARGS)
        if not stray:
            return
        raise TypeError(
            f"{cls.__name__}.refresh() cannot forward {', '.join(stray)}: "
            f"the three edit endpoints refresh() chooses between at runtime "
            f"do not all accept it, so whether the call works would depend on "
            f"which one ran. Portable kwargs: "
            f"{', '.join(sorted(cls._PORTABLE_EDIT_KWARGS))}.\n"
            f"  Fix: edit self.message directly when the view is not being "
            f"re-rendered, or drop the argument."
        )

    def _freeze_edit_kwargs(self) -> Dict[str, Any]:
        """Edit kwargs for a teardown edit that ships the frozen tree.

        The freeze paths (``on_timeout``, ``exit()``'s V2 branch, the
        empty-stack back clear, the reopen fallback) hand Discord the same
        mention-bearing tree ``refresh()`` does, so they owe the same
        mention rules. Without them the last edit a view ever makes is the
        one edit that ignores the view's own ``allowed_mentions``.
        """
        kwargs: Dict[str, Any] = {"view": self}
        mentions = self._resolve_allowed_mentions(None)
        if mentions is not None:
            kwargs["allowed_mentions"] = mentions
        return kwargs

    def _resolve_allowed_mentions(
        self, explicit: Optional[discord.AllowedMentions] = None
    ) -> Optional[discord.AllowedMentions]:
        """Pick the mention rules for one send or edit.

        An explicit argument wins, then the ``allowed_mentions`` class
        attribute, then the bot's own client-level rules.

        That last tier is not redundant. discord.py threads
        ``Client.allowed_mentions`` into every send and into two of the
        three edit endpoints on its own, but ``Message.edit`` forwards it
        only when ``content`` is supplied, and a V2 view never supplies
        content, because the tree is the content. Without this fallback a
        bot that configured suppression globally would still get it on
        send and lose it on any refresh that took the channel endpoint.
        """
        if explicit is not None:
            return explicit
        if self.allowed_mentions is not None:
            return self.allowed_mentions
        # ``_bot`` covers the restored persistent view, which has neither an
        # interaction nor a context and would otherwise lose the client's
        # rules on every post-restart refresh.
        client = (
            getattr(self.interaction, "client", None)
            or getattr(self.context, "bot", None)
            or getattr(self, "_bot", None)
        )
        rules = getattr(client, "allowed_mentions", None)
        # Type-checked rather than truthiness-checked: the attribute is
        # read off whatever object the caller handed in as a client, and
        # only a real AllowedMentions is safe to put on the wire.
        return rules if isinstance(rules, discord.AllowedMentions) else None

    def _sync_back_buttons(self) -> None:
        """Disable every Back button this view carries when the stack is empty.

        A Back button is only meaningful with somewhere to go back to, and
        the paginated and wizard controls beside it already derive their
        disabled state from their own cursor. This is the same rule applied
        to the navigation stack.

        Runs at the render seams rather than at construction because the
        stack is not known when the button is built: ``_navigate_to``
        constructs the destination view and assigns ``_nav_stack``
        afterwards, so a pushed view that composes its tree in ``__init__``
        would read an empty stack and disable a button that works.
        """
        for item in self.walk_children():
            if getattr(item, "_cascadeui_back_button", False):
                item.disabled = not self._nav_stack

    def _apply_theme_defaults(self) -> None:
        """Resolve theme-managed accents against the view's live theme.

        ``card()`` / ``stats_card()`` mark the Containers they build
        without an explicit ``color=``; this resolver stamps the view
        theme's ``accent_colour`` onto every marked top-level Container.
        Runs at the same seams as ``_check_placement`` (initial send,
        refresh, navigation edit), so a marked card renders themed no
        matter where its tree was built (page pre-builds, ``on_load``,
        formatters, module-level helpers), and a runtime theme change
        re-resolves on the next refresh. Explicit colors are never
        marked, so they always win. V1 views hold no Containers and
        fall through untouched.
        """
        theme = self.get_theme()
        if theme is None:
            return
        accent = theme.get_style("accent_colour")
        for child in self.children:
            if isinstance(child, Container) and getattr(child, "_cascadeui_theme_accent", False):
                child.accent_color = accent

    def validate(self) -> None:
        """Raise if this view's tree is one Discord would reject.

        Runs the same checks the library runs before every send,
        refresh, and navigation edit: custom_id uniqueness and length on
        every interactive node, and, for V2 views, the structural
        placement walk. Passing means the tree ships.

        The point of a public entry is testing a view without a Discord
        connection. Compose the tree first, since a view built by a
        pattern is empty until its data loads::

            from cascadeui import MAX_MESSAGE_COMPONENTS
            from cascadeui.testing import stub_client

            view = MyLeaderboard(user_id=1, guild_id=2, bot=stub_client())
            await view.on_load()          # composes the tree
            view.validate()               # raises if Discord would reject it
            assert view.total_components <= MAX_MESSAGE_COMPONENTS

        ``on_load()`` is the render seam the library itself drives, so a
        tree built this way is the tree that would be sent. Views that
        build in ``build_ui()`` call that instead.

        Bind a client to any pattern whose composition depends on one, or
        the tree measured is not the tree that ships: a section-mode
        leaderboard renders a four-node ``image_section`` per row with a
        client bound and a one-node ``TextDisplay`` without, which is
        fifteen components on a five-row page.
        :func:`cascadeui.testing.stub_client` opens no connection and
        reports the empty user cache a live bot reports for an unseen
        member, so the composed tree matches. A persistent view takes its
        client through ``on_bind`` rather than a kwarg.

        ``validate()`` counts nothing, by design: a tree can never exist
        over the per-message cap, because ``add_item`` refuses the node
        that would cross it. :attr:`total_components` is the budget read.

        Raises:
            ValueError: The first violation found, naming the component,
                the path through the tree, and the fix.
        """
        self._check_placement()

    @staticmethod
    def _iter_send_embeds(send_kwargs: dict):
        """Yield every embed a send carries, from either keyword."""
        embed = send_kwargs.get("embed")
        if embed is not None:
            yield embed
        for embed in send_kwargs.get("embeds") or ():
            yield embed

    def _warn_unmatched_attachment_refs(self, send_kwargs: dict) -> None:
        """Warn when the tree names an attachment this send does not carry.

        An ``attachment://<filename>`` reference is half of an upload: the
        matching :class:`discord.File` travels through ``send(files=[...])``
        and Discord resolves the two by name. With no matching file the
        message ships and renders an unresolved placeholder, raising
        nothing and returning no error, so the mistake is invisible to
        every other seam. The initial send is the only place both halves
        are in scope, which is what makes it decidable here and nowhere
        else.

        A warning rather than a raise: Discord's own filename matching is
        the authority, and refusing a reference it would have resolved
        would reject a working message to prevent a cosmetic one.
        """
        wanted = set()
        # V1 carries its references in embeds rather than in the component
        # tree, and the same unresolved placeholder renders there: Discord
        # matches by filename and says nothing when it cannot.
        for embed in self._iter_send_embeds(send_kwargs):
            for url in (
                getattr(getattr(embed, "image", None), "url", None),
                getattr(getattr(embed, "thumbnail", None), "url", None),
                getattr(getattr(embed, "author", None), "icon_url", None),
                getattr(getattr(embed, "footer", None), "icon_url", None),
            ):
                if isinstance(url, str) and url.startswith("attachment://"):
                    wanted.add(url)
        for item in self.walk_children():
            if isinstance(item, (Thumbnail, UIFile)):
                url = getattr(getattr(item, "media", None), "url", None)
                if isinstance(url, str) and url.startswith("attachment://"):
                    wanted.add(url)
            elif isinstance(item, MediaGallery):
                for entry in item.items:
                    url = getattr(getattr(entry, "media", None), "url", None)
                    if isinstance(url, str) and url.startswith("attachment://"):
                        wanted.add(url)
        if not wanted:
            return

        supplied = []
        if send_kwargs.get("file") is not None:
            supplied.append(send_kwargs["file"])
        supplied.extend(send_kwargs.get("files") or ())
        # Compare against File.uri rather than filename: it is the string the
        # media builders emit from a File, spoiler prefix included, so the two
        # sides are built the same way.
        carried = {f.uri for f in supplied if isinstance(f, discord.File)}

        missing = sorted(wanted - carried)
        if missing:
            logger.warning(
                f"{type(self).__name__}: {len(missing)} attachment reference(s) "
                f"have no matching discord.File in this send: "
                f"{', '.join(missing)}. Discord renders these as unresolved "
                f"placeholders. Pass the matching files via send(files=[...])."
            )

    def _check_placement(self) -> None:
        """Validate the component tree before shipping it to Discord.

        Single helper consumed by every seam that ships a tree to
        Discord: ``_send_pipeline`` (initial send), ``refresh`` (in-place
        edits), and ``_apply_navigation_edit`` (push/pop edits). The
        custom_id-uniqueness pass runs for every view (V1 and V2)
        because Discord rejects a duplicate custom_id on either with
        HTTP 400. The structural placement walk is V2-only:
        V1 views lack the ``validate_placement`` attribute so the
        ``getattr`` default of ``False`` skips it. Both imports are lazy
        to keep ``base.py``'s import graph thin. The checks fire often
        (one walk per edit) but load their module once.
        """
        from ._placement import validate_unique_custom_ids, validate_unique_ids

        validate_unique_custom_ids(self)
        # Ungated like the custom_id walk: ``id`` is legal on every component
        # in either tree, so a V1 view can carry a duplicate just as a V2 one can.
        validate_unique_ids(self)
        if getattr(self, "validate_placement", False):
            from ._placement import validate_placement

            validate_placement(self)
        self._warn_over_text_budget()

    def _warn_over_text_budget(self) -> None:
        """Report a V2 tree whose summed display text is over Discord's cap.

        The per-node check the placement validator runs cannot see this:
        no single node has to be over its own cap for the sum to cross the
        message's. Nothing enforces the total either, so the tree ships
        and the refusal arrives at send -- for a persistent panel, long
        after the code that composed it ran.

        A warning rather than a rejection. Discord documents the cap and
        discord.py counts it without raising, so the enforcing side is
        unobserved from here; a raise would reject trees Discord takes.
        Once per view, since the seams that call this run on every edit,
        and re-armed once the tree drops back under, so a later excursion
        is reported rather than swallowed by the first.

        ``content_length`` sums ``TextDisplay`` content only, so silence
        here is not proof a tree is under the cap: a screen carrying its
        text in button labels and select placeholders reads as zero. The
        warning catches the text-heavy shape and cannot see the
        control-heavy one.
        """
        content_length = getattr(self, "content_length", None)
        if content_length is None:
            return
        try:
            total = content_length() if callable(content_length) else int(content_length)
        except Exception:  # noqa: BLE001 -- a counter that raises is not a budget signal
            return
        if total <= MAX_MESSAGE_CHARACTERS:
            # The total is measured before the latch is read, because the
            # other order makes this reset unreachable and leaves the
            # second excursion silent.
            self._text_budget_warned = False
            return
        if self._text_budget_warned:
            return
        self._text_budget_warned = True
        logger.warning(
            f"{type(self).__name__} carries {total} display characters across its "
            f"items, over Discord's {MAX_MESSAGE_CHARACTERS}-character message cap. "
            f"Discord may refuse the send with no component named. Shorten the text, "
            f"or move some of it to a second message."
        )

    def _stamp_cooldown(self, *, acting: bool = False) -> None:
        """Arm the proactive cooldown window after a successful edit.

        No-op when ``refresh_cooldown_ms`` is ``None``, so zero-config
        views never touch the throttle state on the hot path.

        Also a no-op for an *acting* edit. The window paces the library's
        own background re-renders, and an edit a user just asked for is not
        one of them. Stamping it here would let someone holding a button
        push the window ahead of every background reload indefinitely,
        starving the updates the cooldown was configured to pace.
        """
        if acting:
            return
        if self.refresh_cooldown_ms:
            self._cooldown_not_before = time.monotonic() + (self.refresh_cooldown_ms / 1000)

    async def _deferred_refresh(self, wait: float) -> None:
        """Sleep until the cooldown boundary, then re-render.

        Re-enters :meth:`reload` when a reload was coalesced into this window
        (so the deferred render re-fetches via ``on_load``), otherwise
        :meth:`on_state_changed` (so ``build_ui()`` re-runs against the latest
        store state). Either way the deferred edit ships what the view should
        look like *at the moment it fires*, not what it looked like when the
        cooldown kicked in.

        An armed view is the exception: its tree is deliberately frozen on
        the refresh button, so the edit ships as-is with no rebuild.
        ``_handle_state_notification`` enforces that freeze for notifications,
        but this path calls ``on_state_changed`` directly and would otherwise
        walk straight past it.

        The sleep re-checks the window rather than trusting the *wait* it was
        handed, because the window can grow while this task sleeps (a 429 on
        a concurrent edit, a background stamp). Waking into a still-active
        window and simply re-entering :meth:`refresh` would lose the edit
        outright: the gate finds this very task registered as the pending
        retry, declines to schedule a replacement, and the ``finally`` below
        then clears the last reference to it. Nothing is left to ship the
        edit and nothing reports that.
        """
        # A deferred render is background work by definition: whatever click
        # spawned it has long since been acked, and its response slot is
        # spent. ``asyncio.create_task`` copies the caller's context, so the
        # interaction bound at scheduling time would otherwise still be
        # readable here -- and the render would answer to it, claiming the
        # acting waiver it is no longer owed, skipping the cooldown stamp,
        # and editing through a response slot whose ack window has closed.
        # Clearing it is safe: the task holds its own copy of the context,
        # so this is invisible to the caller.
        _CURRENT_INTERACTION.set(None)
        try:
            while True:
                await asyncio.sleep(wait)
                if self.is_finished() or not self._message:
                    return
                wait = self._throttle_until() - time.monotonic()
                if wait <= 0:
                    break
            if self._refresh_armed:
                # Rebuilding here would clear the refresh button, and the
                # armed flag then drops every notification that could put it
                # back -- the user would be left with a stale panel and no
                # recovery path once the webhook token expires.
                await self.refresh()
                if self.refresh_degraded:
                    # Keep trying for as long as the token can still carry an
                    # edit. A 429 re-queues itself through _handle_rate_limit,
                    # so without this a transport drop was the one failure that
                    # gave up after a single retry and froze the panel with
                    # most of the ~90s arming window unspent. Bounded by the
                    # cliff itself: past T+900s the edit answers 401, which
                    # this flag does not report, and the chain ends.
                    self._queue_deferred_refresh(_ARMING_RETRY_SECONDS)
            elif self._reload_pending:
                self._reload_pending = False
                kwargs = self._pending_reload_kwargs
                self._pending_reload_kwargs = {}
                await self.reload(**kwargs)
            else:
                await self._run_state_changed(self.state_store.state)
            if (
                self._reload_pending
                and not self._refresh_armed
                and not self.is_finished()
                and self._message
            ):
                # A reload was coalesced while the render above was in
                # flight: its gate found this task alive and declined to
                # queue a second one, but this task is already past its own
                # dispatch and would otherwise exit leaving the pending
                # reload latched with no task left to run it. Queue a
                # successor (the slot helper allows self-replacement) so the
                # coalesced fetch still ships. Skipped while armed: an armed
                # view's reload is a plain refresh of the frozen tree, and
                # requeueing on a flag the armed branch never clears would
                # respawn successors until the token cliff.
                self._queue_deferred_refresh(max(0.0, self._throttle_until() - time.monotonic()))
        finally:
            # Only disown the slot if it still points at this task. The render
            # above can be rate-limited, and that path schedules a successor
            # into this same slot -- clearing it unconditionally would drop the
            # replacement and leave nothing to ship the edit.
            if self._deferred_refresh_task is asyncio.current_task():
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
        """Return the identifier kwargs for this view's own ``state_scope``.

        The no-overrides case of :meth:`_resolve_scoped_identifiers`,
        which is where the four scope values are actually handled.
        Raises ``ValueError`` when ``state_scope`` is unset or a required
        identifier is missing.
        """
        if self.state_scope is None:
            raise ValueError("Cannot resolve scope target: view has no state_scope set")
        return self._resolve_scoped_identifiers(self.state_scope, {})

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
        return await self.dispatch_scoped_as("SCOPED_UPDATE", data, scope=scope, **identifiers)

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
        # Built through the creator rather than inline, so the one payload
        # shape every scoped reducer decodes has a single author.
        payload = ActionCreators.scoped_update(
            effective_scope,
            effective_ids,
            data,
            slot_name=self._effective_scoped_slot,
        )
        return await self.dispatch(action_type, payload)

    # // ========================================( Session Limiting )======================================== // #

    async def _enforce_instance_limit(self):
        """Enforce instance limiting before sending.

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
        # Exclude the view this send is replacing: an ephemeral reopen swaps a
        # fresh instance in for the dying one, so counting the still-registered
        # old instance would trigger a self-replace that exits it mid-send --
        # and the post-send identity carries (children, undo, session) would
        # then find it already gone.
        replacing_id = getattr(self, "_replacing_view_id", None)
        if replacing_id is not None:
            existing = [v for v in existing if v.id != replacing_id]
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
            await old_view._call_hook_safe(old_view.on_replaced)

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

        # Falsy ids are treated as absent here, matching how the instance
        # index itself keys views (see StateStore._build_instance_scope_key).
        scope_key = store.scope_key(
            cls.instance_scope, user_id=user_id or None, guild_id=guild_id or None
        )
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

        Participants are tracked in the instance index so that instance limiting
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
                        await await_maybe(self.on_instance_limit(error))
                    finally:
                        self.interaction = saved_interaction
                    return False

        # 2. View capacity overflow check (participant_limit). Owner counts.
        if self.participant_limit is not None:
            current = len(self._participants) + (1 if self.user_id is not None else 0)
            if current >= self.participant_limit:
                await await_maybe(self.on_participant_limit(user_id, interaction=interaction))
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

        This is the teardown seam for a live view, not discord.py's
        ``stop()``: ``stop()`` only cancels the timeout task and leaves
        the view registered in the active-view registry, so a view stopped
        without ``exit()`` (or a natural timeout) leaks that entry. A static
        display view freezes nothing, so ``exit(delete_message=False)`` tears
        down its state without a cosmetic message edit.
        """
        if delete_message is None:
            delete_message = self.exit_policy == "delete"
        # Exit tracked child views first
        await self._cleanup_attached_children()

        # Cancel all tasks owned by this view
        self.task_manager.cancel_tasks(self.id)

        # Stop this view
        self.stop()

        # Unsubscribe before tearing down so the view's own subscriber does
        # not react to its own VIEW_DESTROYED.
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        # Tear down both registries before the cosmetic message edit.
        # _destroy_view frees the instance-limit slot up front so a concurrent
        # send() does not count this exiting view, and removes the state entry
        # before the active entry so a failed dispatch cannot strand a ghost.
        # The up-to-edit_timeout edit below then runs with the view already
        # gone from state and _active_views.
        await self.state_store._destroy_view(self.id, source_id=self.id)

        # Clean up the message: freeze the V2 tree, strip V1 buttons, or delete
        # outright on Discord's side.
        if self._message:
            try:
                if delete_message:
                    await self._bounded(self._message.delete())
                elif self._is_layout():
                    # V2 messages ARE their components -- edit(view=None) would
                    # produce an empty message (error 50006). Freeze instead.
                    # The edit is skipped only when it would ship what is
                    # already on screen: nothing froze AND the tree still
                    # matches the last render. Testing the freeze alone read
                    # a caller who rebuilt the tree before exiting as having
                    # nothing to say, so a farewell card composed in
                    # on_timeout was dropped and the expired prompt kept its
                    # live-looking buttons until someone pressed one.
                    froze = self._freeze_components()
                    if froze or self._compute_tree_digest() != self._last_tree_digest:
                        await self._bounded(self._message.edit(**self._freeze_edit_kwargs()))
                else:
                    await self._bounded(self._message.edit(view=None))
            except discord.NotFound:
                # Expected lifecycle: user dismissed the ephemeral, an
                # admin deleted the message, or the channel was deleted.
                # Nothing left to clean up on Discord's side.
                pass
            except asyncio.TimeoutError as e:
                # State teardown already ran above, so the view is gone either
                # way and only the diagnostic differs. aiohttp's connect and
                # socket timeouts inherit both and fire below edit_timeout, so
                # the stall message would name a ceiling that was never reached
                # -- and reads as "stalled past None s" when edit_timeout is off.
                if isinstance(e, aiohttp.ClientError):
                    logger.warning(
                        f"Exit cleanup edit did not reach Discord for "
                        f"{type(self).__name__}: {describe_discord_error(e)}. "
                        f"View torn down regardless."
                    )
                else:
                    logger.warning(
                        f"Exit cleanup edit stalled past {self.edit_timeout}s for "
                        f"{type(self).__name__}; view torn down regardless."
                    )
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

        return True

    def make_exit_button(
        self,
        label="Exit",
        style=discord.ButtonStyle.secondary,
        emoji="\u274c",
        delete_message=None,
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

        ``delete_message`` defaults to ``None``, which forwards the
        decision to the view's ``exit_policy``: ``"disable"`` freezes the
        components, ``"delete"`` removes the message. Pass an explicit
        ``True`` or ``False`` to override the policy for this button.

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
        delete_message=None,
        custom_id=None,
    ):
        """Add a button that exits this view when clicked.

        Thin wrapper over :meth:`make_exit_button` that attaches the
        result to ``self``. ``delete_message=None`` (the default) defers
        to the view's ``exit_policy``. For PersistentView subclasses,
        pass a custom_id (e.g. ``custom_id="exit"``).
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

    def make_back_button(
        self,
        label="Back",
        style=discord.ButtonStyle.secondary,
        emoji="◀",
        custom_id=None,
        row=None,
    ):
        """Return a back button without attaching it to the view.

        Mirrors :meth:`make_exit_button`. The callback pops the navigation
        stack via :meth:`pop`. Pack the returned button into a caller-owned
        container -- an ``ActionRow`` or a tab builder's return list (V2
        views can also use ``make_nav_row`` to build the Back+Exit footer in
        one call).

        The button renders disabled while the stack is empty, resolved at
        each render seam rather than here: a pushed view is constructed
        before ``_navigate_to`` assigns its stack, so reading it at build
        time would disable a button that works. A press that still reaches
        an empty stack (a persistent view restored after a restart, whose
        message shows the pre-restart render) is acknowledged without
        modifying the message. Override ``_clear_on_empty_back`` to close
        the panel on Back instead, though :meth:`exit` and the Exit button
        are the surfaces built for that.

        When the destination view defines :meth:`on_load`, the restored
        parent reloads its DATA automatically on pop, so the back button
        needs no rebuild wiring for that. Selection state is a separate
        question: ``pop`` reconstructs the parent rather than restoring it,
        so anything chosen after construction returns to its default unless
        the view names it in :meth:`get_nav_state`.

        For ``PersistentView``/``PersistentLayoutView`` subclasses, pass
        ``custom_id`` so the button survives a restart.
        """

        async def back_callback(interaction):
            # No pre-defer: pop() routes through _apply_navigation_edit, whose
            # fast path edits + acks in one round-trip. Deferring here would
            # consume the response slot and force the slow two-call path.
            prev_view = await self.pop(interaction)
            if prev_view is None:
                await self._clear_on_empty_back(interaction)

        button = StatefulButton(
            label=label,
            style=style,
            row=row,
            emoji=emoji,
            custom_id=custom_id,
            callback=back_callback,
        )
        # Marked so the render seams can disable it when the stack is empty.
        # The stack is not readable here: a pushed view is constructed before
        # ``_navigate_to`` assigns it.
        button._cascadeui_back_button = True
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
