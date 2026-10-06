# // ========================================( Modules )======================================== // #


import asyncio
import contextlib
import contextvars
import functools
import hashlib
import inspect
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, ClassVar, Dict, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

import aiohttp
import discord
from discord import Interaction
from discord.components import _component_factory
from discord.ui import ActionRow, Button, Container, DynamicItem
from discord.ui import File as UIFile
from discord.ui import Item, MediaGallery, Separator, TextDisplay, Thumbnail
from discord.ui.select import BaseSelect

from ..components.base import StatefulButton, _dynamic_button_classes
from ..components.types import MAX_MESSAGE_CHARACTERS, EmojiInput
from ..exceptions import InstanceLimitError
from ..state.actions import ActionCreators
from ..state.singleton import get_store
from ..state.store import _CURRENT_INTERACTION, _SOURCE_VIEW
from ..utils.coercion import coerce_snowflake_id, coerce_snowflake_id_set, is_snowflake
from ..utils.deprecation import REMOVED_IN, warn_deprecated
from ..utils.hooks import await_maybe, call_hook_safe, is_async_callable
from ..utils.responses import (
    DISCORD_CALL_ERRORS,
    _note_stalled_render,
    _rewind_files,
    ack_backstop,
    describe_discord_error,
    validate_ack_delay,
)
from ..utils.tasks import (
    _bounded_wait,
    _in_teardown_scope,
    _teardown_scope,
    get_task_manager,
)
from ._custom_ids import item_signature
from ._interaction import _ARMING_RETRY_SECONDS, _InteractionMixin
from ._navigation import _NAVIGATION_TURN, _NavigationMixin

logger = logging.getLogger(__name__)

# Discord's custom_id ceiling. discord.py stores the value unchecked.
_CUSTOM_ID_MAX_CHARS = 100

# How many recent button signatures a view remembers the numbers of.
_CUSTOM_ID_TOKEN_HISTORY = 256


# // ========================================( View Registry )======================================== // #


# Maps class name -> class for navigation stack resolution
_view_class_registry: Dict[str, type] = {}


def _class_for_session_key(key: str) -> Optional[type]:
    """The registered view class whose session key is ``key``, or ``None``."""
    return next((c for c in _view_class_registry.values() if c._class_session_key() == key), None)


# A pushed view's root limit not carried yet (None is a limit: unlimited).
_ROOT_LIMIT_UNKNOWN = object()


def _stack_seqs(stack) -> tuple:
    """The commit stamps of an undo or redo stack's entries, in stack order."""
    return tuple(entry.get("seq", -1) for entry in stack or ())


# Class names already warned about an author locked out of their own view.
# Dedupes to once per class so repeat opens do not spam the log.
_user_id_lock_out_warned: set = set()

# Tracks view classes whose on_load() has overrun the interaction-timing budget.
# Deduped per class so a consistently-slow preload warns once, not on every
# send/navigation.
_slow_on_load_warned: set = set()

# Sibling of _slow_on_load_warned for the reactive rebuild seam (on_state_changed).
_slow_render_warned: set = set()

# Task -> the view whose reload lock it is queued on. A reload about to queue
# follows holder -> queued-on -> holder through it; reaching its own task means
# the wait could never end.
_RELOAD_WAITING: Dict[asyncio.Task, Any] = {}

# How long a wait for a view's reload turn runs before it is logged. The cycle
# walk cannot see a holder that awaits a child task instead of a lock.
_RELOAD_WAIT_WARN_SECONDS = 30.0

# Task -> the view whose close it is waiting to run. A close about to wait
# follows holder -> waited-on view -> holder through it; reaching its own task
# means the holder waits on it, so it leaves its request with the holder.
_CLOSE_WAITING: Dict[asyncio.Task, Any] = {}

# How long a close waits for another close of the same view before it leaves
# its request with that one, so a wait the walk cannot see through still ends.
_CLOSE_WAIT_SECONDS = 30.0

# How long a send holds a cancel landing after Discord accepted its message
# while it finishes, so a step that never returns cannot keep the cancel.
_CANCEL_HOLD_SECONDS = 5.0

# How far a close takes the view's message, in increasing order: a later
# close owes only the part an earlier teardown left undone.
_MESSAGE_LEAVE, _MESSAGE_FREEZE, _MESSAGE_DELETE = 0, 1, 2

# Kwargs that are ephemeral per-invocation and must NOT be saved for
# push/pop reconstruction.  _navigate_to() re-supplies these when
# building the next view, so persisting them would be wrong.
_NON_RECONSTRUCTIBLE_KWARGS = frozenset(
    {
        "context",
        "interaction",
        "state_store",
        "session_id",
        "user_id",
        "guild_id",
        "parent",
    }
)

# Backoff for a 429 with no ``Retry-After``. discord.py retries ordinary rate
# limits itself; the 429 it raises without one is its Cloudflare case (no
# ``Via`` header, or a body that is not JSON): the whole bot blocked at the
# edge, typically for hours, acks included. So this sets how often a banned
# bot knocks, not UI pacing, and each 429 counts toward the budget that set the
# ban: minutes, not seconds.
_CLOUDFLARE_BAN_BACKOFF = 60.0

# Task-manager owner for teardowns scheduled when an edit finds a view's
# message deleted. Not the view's own id: exit() cancels that owner's tasks.
_MESSAGE_GONE_TASK_OWNER = "view_message_gone"

# Task-manager owner for closes resumed after the holder they were left with
# was cancelled. Not the view's own id, for the same reason.
_CLOSE_RESUME_TASK_OWNER = "view_close_resume"

# Interactions whose response can update the message they came from.
_ACTING_INTERACTION_TYPES = (
    discord.InteractionType.component,
    discord.InteractionType.modal_submit,
)

# A render made later than it was asked for: (view, the number of the render
# it stands for, whether it is that render sent again, the task running it,
# a cell holding True until it returns). See _late_render. One sent again (a
# stalled answer, content held during a send) skips the cooldown, whose
# deferred re-render runs from state and would drop the keywords it carries.
_RENDER_ORIGIN: contextvars.ContextVar = contextvars.ContextVar(
    "cascadeui_render_origin", default=None
)


def _newer_render(seq: int, at: int, resending: bool) -> bool:
    """Whether render ``seq`` replaces what the render numbered ``at`` set.

    Every refresh() of one late render shares its number, and the later one
    wins. A render sent again carries its first attempt's number, and that
    attempt already set what it sets.
    """
    return seq > at or (seq == at and not resending)


# Edit keywords that carry content outside the component tree, mapped to the
# part of the message each sets. Every render ships the tree as it stands, so
# only these make one render's edit differ from another's beyond it (a V1
# embed, a message's files).
_CONTENT_EDIT_FIELDS = {
    "content": "content",
    "embed": "embeds",
    "embeds": "embeds",
    "attachments": "attachments",
}


def _merge_close(request, exit: bool, message: int) -> Tuple[bool, int]:
    """A close request that asks for everything ``request`` and the new one ask."""
    if request is not None:
        exit = exit or request[0]
        message = max(message, request[1])
    return exit, message


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
    - ``DEFERRED``: no edit has shipped yet; the render runs later, when the
      active cooldown or rate-limit window closes, once a send in progress
      has created the message, or when a failed navigation hands the view
      back.
    - ``DROPPED``: the edit was attempted and is not known to have landed (a
      transport failure, or a request stalled past ``edit_timeout``), and
      nothing is scheduled to retry it. The definitively-dropped case also
      reports through :attr:`_StatefulMixin.refresh_degraded`; a stalled
      request is indeterminate, so only the disposition covers it.
    - ``NO_MESSAGE``: no editable message remains. The view has not been
      sent, the render runs inside the view's own ``send()`` (which ships
      the tree), the message was deleted, an ephemeral's webhook token has
      expired, or a reload found the view torn down (exited, timed out, or
      navigated away from) by the time its turn came. Retrying cannot help.
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


class _HeldCancel:
    """A cancel a send holds while it finishes after Discord accepted its message.

    Held for at most ``_CANCEL_HOLD_SECONDS``: the send is then cancelled
    again, and that cancel goes through, cutting the step running and
    skipping the rest. True once a cancel is held.
    """

    def __init__(self) -> None:
        self.expired = False
        self._task: Optional[asyncio.Task] = None
        self._timer: Optional[asyncio.TimerHandle] = None

    def __bool__(self) -> bool:
        return self._task is not None

    def hold(self) -> None:
        if self._task is None:
            self._task = asyncio.current_task()
            self._timer = asyncio.get_running_loop().call_later(_CANCEL_HOLD_SECONDS, self._expire)

    def _expire(self) -> None:
        self.expired = True
        self._task.cancel()

    def release(self) -> None:
        """Stop the timer, and take back its cancel if it fired.

        Taken back so the caller's ``asyncio.timeout()`` still counts only its
        own cancel and reports ``TimeoutError``.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
            if self.expired and hasattr(self._task, "uncancel"):
                self._task.uncancel()


async def _hold_cancel(awaitable: Any, cut: _HeldCancel) -> Any:
    """Await ``awaitable``, holding a cancel landing in it in ``cut`` instead of raising it.

    A send runs the steps after its message was posted this way and raises
    the cancel once they are done. Once the hold has run out, no further step
    starts.
    """
    if cut.expired:
        # Also reached when a step swallowed the cancel the hold ran out on.
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.CancelledError()
    try:
        return await awaitable
    except asyncio.CancelledError:
        cut.hold()
        return None


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
    # ``scoped_state`` and ``dispatch_scoped``. Distinct from
    # ``instance_scope``, which governs session-limit indexing.
    state_scope: Optional[str] = None

    # Subclass config: the slot under ``state["application"]`` this view's
    # scoped writes land in; ``None`` uses the shared ``"scoped"`` slot.
    scoped_slot: Optional[str] = None

    # Subclass config: slot names saved to disk, registered at class
    # definition (as ``access_slot(..., persistent=True)`` would).
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
    # Sent when a user hits the instance limit; falsy uses
    # ``InstanceLimitError.default_message``. Override on_instance_limit for more.
    instance_limit_message: Optional[str] = None

    # Subclass config: users one view can hold, owner included (a lobby cap),
    # unlike instance_limit, which counts views. ``None`` is unlimited.
    participant_limit: Optional[int] = None
    # Sent to a joiner past the cap. Override on_participant_limit for more.
    participant_limit_message: str = "This session is full."

    # Subclass config: send() registers every non-owner in ``allowed_users``
    # as a participant, all or nothing, before posting.
    auto_register_participants: bool = False

    # Subclass config: a view holding other users' participants or children
    # is not replaced by the instance limit; when too few other views can be
    # replaced, the new view is rejected instead.
    protect_attached: bool = True

    # Subclass config: interaction ownership
    owner_only: bool = True  # Reject interactions from non-owners
    # Sent to a user the access check refuses. Override on_unauthorized for more.
    unauthorized_message: str = "You cannot interact with this."
    # The description in on_error's default red embed. Override on_error for more.
    error_message: str = "An unexpected error occurred while processing your interaction."

    # Assignment coerces Members, Users, and Objects to ids, so
    # ``{ctx.author, opponent}`` works. ``None`` falls back to owner_only.
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
        cycles, clean re-parenting). A parent that handed its panel on (a
        ``push()`` or ``pop()``, or an ephemeral panel's Continue button)
        reads as the view that took its place.
        """
        if self._attached_to is not None:
            return self._attached_to
        if self._pending_parent is not None:
            return self._pending_parent._last_successor()
        return None

    # Subclass config: auto-defer safety net
    auto_defer: bool = True
    # Seconds before the timer acks an unanswered interaction, inside
    # Discord's 3s deadline (counted from the interaction's creation). The
    # one-request edit budgets are this minus 1.0.
    auto_defer_delay: float = 2.5
    # Ack before the access checks and the callback, for a callback that
    # blocks the loop. Costs the one-request refresh, and open_modal() then
    # falls back to an ephemeral message.
    ack_first: bool = False

    # Subclass config: minimum gap in milliseconds between background edits;
    # renders inside it coalesce into one at the boundary. The 429 backoff
    # is separate and always on.
    refresh_cooldown_ms: Optional[int] = None

    # Subclass config: seconds a Discord edit may stall before it is
    # cancelled. discord.py passes no timeout, so ``None`` leaves only
    # aiohttp's default of five minutes per request.
    edit_timeout: Optional[float] = 60.0

    # Subclass config: run a view's clicks one at a time.
    serialize_interactions: bool = True

    # Subclass config: repeat opens of the class by one user share a session
    # (undo, shared_data) instead of each getting its own.
    session_continuity: ClassVar[bool] = False

    # Subclass config: swap in a Continue button before an ephemeral's
    # 15-minute token expires. ``None`` derives from ``timeout`` at each send
    # (engaged above 900s or with no timeout). The library never assigns it.
    auto_refresh_ephemeral: Optional[bool] = None
    refresh_warning_seconds: int = 90  # how early to swap before the 900s wall
    refresh_button_label: str = "Continue Session"
    refresh_button_emoji: EmojiInput = "\U0001f504"  # 🔄
    refresh_button_style: discord.ButtonStyle = discord.ButtonStyle.primary
    # Sent when the Continue button cannot build a replacement. Override
    # on_reopen_failure for more.
    reopen_failure_message: str = (
        "Could not refresh this view. Please reopen from the original command."
    )

    # Sent when a modal is submitted after this view closed, or when the
    # refresh button's reopen returns None. ``None`` sends nothing.
    session_ended_message: Optional[str] = "This session has ended."

    # What the instance limit's replacement does to the old view's message:
    # "delete", or "disable" (V2 freezes the components, V1 removes them).
    # Only that transition reads it.
    replace_policy: str = "delete"
    # Sent to the channel when a view with participants is replaced; ``None``
    # is silent. Override on_replaced for more.
    replaced_message: Optional[str] = None

    # What exit() does to the message when no delete_message is passed, and
    # how a view sent again closes the message it left. on_timeout() does not
    # read it.
    exit_policy: str = "disable"

    # Mention rules for this view's message, on the send and every refresh.
    # ``None`` uses the client's. Unrelated to allowed_users.
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
    # Ack-backstop delays: a positive number under Discord's 3s ack deadline.
    _ACK_DELAY_ATTRS: ClassVar[tuple] = ("auto_defer_delay",)
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
    # ``str.format`` templates, mapped to test kwargs of the render-time
    # types: a spec can suit one type and not another (``{n:.2f}``).
    _FORMAT_ATTRS: ClassVar[dict] = {}
    # Snowflake-domain instance data -- coerced via the init pipeline,
    # never settable through set_class_attribute (those have their own
    # mutation paths and live as instance state, not class-level policy).
    _INSTANCE_DATA_ATTRS: ClassVar[frozenset] = frozenset(
        {"user_id", "guild_id", "allowed_users", "_participants"}
    )
    # Read from the class, not the instance: at class definition, inside
    # __init__, or from a classmethod with no instance. A per-instance value
    # would be accepted and never read, so set_class_attribute refuses them.
    _CLASS_ONLY_ATTRS: ClassVar[tuple] = (
        "session_continuity",
        "persistent_slots",
        "session_class_key",
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
        if name in cls._effective_table("_ACK_DELAY_ATTRS"):
            validate_ack_delay(cls.__name__, name, value)
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
            # A value that binds as a method would receive self, not the
            # destination. Descriptor-ness decides that (a staticmethod is
            # callable too, and the one descriptor that is correct here).
            # functools.partial binds only from 3.14, so it is refused on
            # every version, not just where it breaks.
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
            ("_ACK_DELAY_ATTRS", cls._validate_attribute_value),
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
        # Reversed so a nearer class wins, as attribute lookup does. A dict
        # table (name -> allowed values) carries its values through.
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
        """Reject a text attribute (a label, a title) that is neither a string nor ``None``."""
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
        cls._check_slot_coherence(
            cls.__name__,
            getattr(cls, "scoped_slot", None),
            tuple(getattr(cls, "persistent_slots", ())),
        )

    @staticmethod
    def _check_slot_coherence(owner: str, scoped_slot, persistent_slots) -> None:
        """Raise when scoped writes would go to a slot the view does not persist."""
        if scoped_slot is None:
            return
        if "scoped" not in persistent_slots:
            return
        raise ValueError(
            f'{owner}.persistent_slots includes "scoped" but '
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

        The override takes effect from the view's next render: a pattern
        rebuilds the buttons and layout built from it then, and undo tracking
        and the store subscription follow when it is set. ``pop()`` and the
        Continue button, which rebuild the view from its constructor
        arguments, set it again on the view they build.

        Attributes the library reads from the class are refused too, such as
        ``session_continuity``, which ``__init__`` has already read, and
        ``persistent_slots``, registered when the class is defined.

        Raises ``ValueError`` if the name is unknown to the class, the
        name is instance data, the attribute is read from the class, or
        the value fails validation.
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
        if name in cls._effective_table("_CLASS_ONLY_ATTRS"):
            raise ValueError(
                f"{cls.__name__}.{name} is read from the class, with no view instance "
                f"in hand, so a value set on one instance would never be read. Set it "
                f"on the class body instead."
            )
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
        if name == "scoped_slot":
            # The class body gets this check at definition; persistent_slots
            # is class-only, so the class's value is the one to match.
            cls._check_slot_coherence(
                cls.__name__, value, tuple(getattr(cls, "persistent_slots", ()))
            )
        setattr(self, name, value)
        self._class_overrides[name] = value
        self._apply_class_attribute(name)

    def _apply_class_overrides(self, overrides) -> None:
        """Set again what ``set_class_attribute()`` set on the view this one rebuilds."""
        for name, value in overrides.items():
            self.set_class_attribute(name, value)

    def _apply_class_attribute(self, name: str) -> None:
        """Carry an override into what the view set up from the class value.

        ``__init__`` registers undo tracking and subscribes to the store from
        these, so an override set afterwards would otherwise reach neither.
        Anything built into the view's components is re-read at its next
        render instead.
        """
        if self._torn_down():
            return
        if name in ("enable_undo", "undo_limit"):
            if self.enable_undo:
                self.state_store._undo_enabled_views[self.id] = self.undo_limit
            else:
                self.state_store._undo_enabled_views.pop(self.id, None)
        elif name == "subscribed_actions":
            if self.subscribed_actions is not None:
                self.subscribed_actions = set(self.subscribed_actions)
            callback, _, selector = self.state_store.subscribers[self.id]
            self.state_store.subscribers[self.id] = (callback, self.subscribed_actions, selector)
        elif name == "instance_scope":
            self.state_store._reindex_instance(self)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Validated before the class is registered anywhere, so a definition
        # that is going to be refused is never left resolvable by a later
        # lookup. Subclass hooks further down the chain read the pin to key
        # their own registries, which is why its shape is settled first.
        cls._validate_session_class_key()
        cls._validate_class_attributes()
        _register_view_class(cls)

        # The Continue button's builder was documented as the private
        # _build_refresh_button(); an override of that name is called through
        # the public one until the old name is removed. The override is found
        # the way Python finds a method, so one on a plain mixin counts too; a
        # view class above this one already had its own definition handled.
        owner = next(
            (
                klass
                for klass in cls.__mro__
                if "build_refresh_button" in klass.__dict__
                or "_build_refresh_button" in klass.__dict__
            ),
            None,
        )
        legacy = owner.__dict__.get("_build_refresh_button") if owner is not None else None
        if (
            legacy is not None
            and owner is not _InteractionMixin
            and (owner is cls or not issubclass(owner, _StatefulMixin))
        ):
            if "build_refresh_button" in owner.__dict__:
                note = f"{owner.__qualname__}.build_refresh_button() is the one called"
            else:
                cls.build_refresh_button = legacy
                note = "it is called through the new name until then"
            warn_deprecated(
                f"{owner.__qualname__} overrides _build_refresh_button(), which is "
                f"deprecated and will be removed in {REMOVED_IN}: rename it "
                f"build_refresh_button(); {note}."
            )

        # A selector runs inline in dispatch, where nothing awaits it, so an
        # async one returns a new coroutine every time and the view is
        # notified on every action, filtering nothing. subscribe() only sees
        # the lambda _build_selector wraps it in, so the check is made here,
        # on the class that declares it.
        own_selector = cls.__dict__.get("state_selector")
        if own_selector is not None and is_async_callable(own_selector):
            raise TypeError(
                f"{cls.__name__}.state_selector must be synchronous; it is compared "
                f"inline during dispatch, which cannot await it, so an async one is "
                f"never resolved and the view is notified on every action."
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

        # Wrap __init__ so pop() can rebuild the view from its kwargs. Keyed
        # on whether a wrapper is inherited, not on cls.__dict__: a pattern
        # can take __init__ from a plain mixin. Only the outermost captures.
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
                    # The view subscribed (and may have registered for undo)
                    # before a body that raised; left, both would keep a view
                    # no caller received alive. Both removals are idempotent.
                    view_id = getattr(self, "id", None)
                    store = getattr(self, "state_store", None)
                    if view_id is not None and store is not None:
                        store._unsubscribe(view_id)
                        store._undo_enabled_views.pop(view_id, None)
                    raise

            _capturing_init._cascadeui_captures_kwargs = True
            cls.__init__ = _capturing_init

        # build_ui runs inside the view's theme (card() and stats_card() read
        # it) and its generated ids are stabilized once the tree is built.
        if "build_ui" in cls.__dict__:
            original_build = cls.build_ui

            # The sync wrapper stays sync: three classes call build_ui() from
            # __init__. iscoroutinefunction misses a callable instance, a
            # partial around one, or a function returning a coroutine, so the sync branch
            # checks the result: with a coroutine in hand nothing is built yet.
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

                    # Re-enters the theme for the body rather than holding it
                    # across the await, which would leak it into whatever else
                    # the awaiting task runs.
                    async def _finish_themed_build():
                        with theme_context(self.get_theme()):
                            value = await result
                            self._stabilize_custom_ids()
                            return value

                    return _finish_themed_build()

            cls.build_ui = _themed_build_ui

        # on_load is the modern preload seam and builds component trees
        # the same way build_ui does, so it gets the same ambient theme.
        # Custom_id stabilization is not repeated here: send, navigation, and
        # refresh() stabilize at their own seams.
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
        # Stored keys come back from the database as strings, so any other type
        # stops matching its own row after a restart.
        if self._persistence_key is not None and not isinstance(self._persistence_key, str):
            raise TypeError(
                f"persistence_key= must be a str, got {type(self._persistence_key).__name__}. "
                f'Fix: build it as a string, e.g. persistence_key=f"panel:{{guild_id}}".'
            )
        if self._persistence_key == "":
            raise ValueError("persistence_key= must not be empty. Fix: pass a non-empty string.")
        _parent = kwargs.pop("parent", None)
        if _parent is not None and not isinstance(_parent, _StatefulMixin):
            raise TypeError(
                f"parent= must be a StatefulView or StatefulLayoutView instance, "
                f"got {type(_parent).__name__}"
            )
        self._pending_parent = _parent

        # The kwargs the __init_subclass__ wrapper captured from the most
        # derived class, before any __init__ consumed them.
        self._init_kwargs = getattr(self, "_pending_init_kwargs", {})
        if hasattr(self, "_pending_init_kwargs"):
            del self._pending_init_kwargs
        else:
            # No wrapper ran -- StatefulView used directly, capture manually
            if self.theme is not None:
                self._init_kwargs["theme"] = self.theme
            if self._persistence_key is not None:
                self._init_kwargs["persistence_key"] = self._persistence_key
        # The kwargs cannot carry these, so a rebuild from them (pop(), the
        # Continue button) sets them again.
        self._class_overrides = {}

        # Initialize the discord.py base class (View or LayoutView)
        super().__init__(*args, **kwargs)

        # Unique identifier for this view instance
        self.id = str(uuid.uuid4())
        # Signature of what a button does -> the number its custom_id carries.
        self._custom_id_tokens: OrderedDict = OrderedDict()
        self._custom_id_token_next = 0

        # _message is the re-fetched channel message after a non-ephemeral
        # send (no token expiry); an ephemeral keeps the interaction's handle.
        # An interaction's own message also keeps _webhook_message, the only
        # handle that can edit its embeds, until the 15-minute token expires.
        self._message = None
        self._webhook_message = None
        self._ephemeral = False
        # When the Continue button arms (refresh_warning_seconds before the
        # send token's 900s window closes), so a re-schedule after a
        # rolled-back navigation sleeps only the time that remains.
        self._ephemeral_arm_deadline = None
        # What auto_refresh_ephemeral=None resolved to (from timeout, or the
        # navigation source). An explicit declaration wins over it.
        self._refresh_handoff_resolved: Optional[bool] = None

        # Digest of the tree last shipped; refresh() skips an edit whose tree
        # matches. None: no baseline, so the next refresh ships.
        self._last_tree_digest: Optional[int] = None
        # True once any tree has landed on Discord; never reset, unlike the
        # digest above. Read by _teardown_edit_target.
        self._has_rendered: bool = False
        # Numbers each refresh() as it begins, and records the newest render
        # that reached the message or stalled, for the tree and for content
        # carried in keywords (see _ship_stalled_render).
        self._render_seq: int = 0
        self._superseded_at: Dict[str, int] = {
            "tree": 0,
            **dict.fromkeys(_CONTENT_EDIT_FIELDS.values(), 0),
        }
        # The number of the newest redraw that rebuilt the tree and reached the
        # message in a teardown edit (a failed navigation's nav_rebuild). A
        # late render asked for before it would ship its tree over it.
        self._redrawn_at: int = 0
        # V2: the mention rules of the newest render that changed the message,
        # its number, and what its tree showed. A V2 edit rebuilds mentions
        # from the whole tree, so every one carries these.
        self._mention_rules: Tuple[Optional[discord.AllowedMentions], int, Any] = (None, 0, None)
        # Digest of the tree __init__ produced, stamped only on persistence
        # reattach. Read by _teardown_edit_target.
        self._reattach_baseline_digest: Optional[int] = None
        # Set when refresh() swallowed a transport failure (refresh_degraded).
        self._refresh_degraded: bool = False

        # Whether state registration has been done
        self._registered = False

        # Session origin: when a view is pushed via navigation, this is set to
        # the root view's class name so the entire nav chain is tracked under
        # one instance index key.  None means this view IS the root.
        self._instance_root_class: Optional[str] = None
        # The root's instance_scope, which keys this view in the instance
        # index while it is part of the root's chain. None means its own.
        self._instance_root_scope: Optional[str] = None
        # The root's instance_limit as it stood on the root instance, so an
        # override set there holds for the chain; unknown until a push carries it.
        self._instance_root_limit = _ROOT_LIMIT_UNKNOWN
        # (source, undo seqs, redo seqs) carried onto this view by a navigation
        # in flight, settled at its commit.
        self._undo_carried = None

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

        self._refresh_armed: bool = False
        # One warning per view: the seams that check it run on every edit.
        self._text_budget_warned: bool = False
        self._reopen_in_flight: bool = False
        # The task running a Continue, and the event its end sets: an exit()
        # or a timeout from another task waits for it, as for a push.
        self._reopen_task: Optional[asyncio.Task] = None
        self._reopen_settled = asyncio.Event()
        self._reopen_settled.set()

        # Get task manager
        self.task_manager = get_task_manager()

        # Interaction serialization lock -- prevents racing message edits
        # from rapid button clicks that cause "This interaction failed"
        self._interaction_lock = asyncio.Lock()

        # Update coalescing -- prevents concurrent on_state_changed calls
        # on the same view when multiple dispatches converge on one subscriber
        self._update_lock = asyncio.Lock()
        self._update_pending = False

        # Two windows, kept apart because only the first is waived for an
        # edit answering a click: the refresh_cooldown_ms pacing, and
        # Discord's 429 backoff. One deferred task serves any number of
        # refreshes inside a window.
        self._cooldown_not_before: float = 0.0
        self._ratelimit_not_before: float = 0.0
        self._deferred_refresh_task: Optional[asyncio.Task] = None
        # The keywords of the last render asked of a torn-down view, which the
        # deferred task ships as the tree stands: it cannot re-render from state.
        self._closed_render_kwargs: Optional[Dict[str, Any]] = None
        self._message_gone_task: Optional[asyncio.Task] = None
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
        self._reload_label: Optional[str] = None
        # A render that could not run while another task held the reload
        # turn or was sending the view (a state notification, a refresh); the
        # turn's release, or the end of the send, replays it.
        self._render_after_turn = False
        # The number of the newest state render deferred to that replay.
        self._deferred_origin = 0
        # Refreshes deferred during a send, or while a push or pop from this
        # view is in flight, by the part of the message each keyword sets (a
        # V1 embed), with the number of the render that asked for it. The
        # replay ships what no newer render has set.
        self._held: Dict[str, Tuple[Dict[str, Any], int]] = {}
        # The replay and redraw tasks scheduled to ship it (_deliver_later).
        self._deliveries: Set[asyncio.Task] = set()
        # V2: _mention_rules as the renders held during a send left them.
        self._held_mention_rules: Optional[Tuple[Any, int, Any]] = None
        # The render number when the running send began: the send's post
        # replaces everything asked for before it.
        self._send_start = 0
        # The task running send() until the message exists. Another task's
        # render waits for it, and the release of its delivery turn replays
        # what was deferred.
        self._sending_task: Optional[asyncio.Task] = None
        # The per-send state a send found on a view already live on a message,
        # put back if that send fails; None for a first send.
        self._live_before_send: Optional[tuple] = None
        # "Away" while a push or pop from this view is in flight: subscribed,
        # but its renders are recorded for a rollback instead of edited, and
        # other closes, clicks and navigations wait on _navigation_settled.
        self._away_for_navigation = False
        self._missed_while_away = False
        self._arm_after_rollback = False
        # The Continue button's arming, declined while a close was under way
        # that could still be undone (a failed replace(), a close cut off).
        self._arm_after_close = False
        self._navigation_task: Optional[asyncio.Task] = None
        self._navigation_settled = asyncio.Event()
        self._navigation_settled.set()
        # Whether the push or pop in flight has sent a request yet, and the
        # destination it built, for the settle.
        self._nav_edit_started = False
        self._navigation_destination = None
        # The view a landed push or pop handed this view's message to.
        self._successor = None
        self._warned_handed_over = False
        # Exits of this view the library is running (cleanup that follows a
        # push it waited out, a Continue closing the view it replaced). No
        # stale reference made them, so the handed-over warning skips them.
        self._library_exits = 0
        # A navigation destination a failed push or pop discarded.
        self._discarded = False
        # A navigation is removing this view's state row, and a close leaves
        # the removal to it.
        self._destroy_in_flight = False
        # A V1 exit removed the controls from the message.
        self._controls_removed = False
        # Set once exit() has begun: the view takes no more clicks or
        # navigation while its children close, before it stops.
        self._closing = False
        # A failed navigation owes this view a redraw that has not run yet; a
        # freeze that begins first ships the content in its own edit.
        self._reclaim_pending = False
        # The redraw found a close under way and left it to that close; an
        # undone close runs it again.
        self._reclaim_declined = False
        # On a push or pop destination, the view whose navigation is bringing
        # it in, until that navigation settles. It does not own the message
        # yet, so renders other than the navigation's own edit are recorded
        # and replayed once it does.
        self._arriving_from = None
        # Renders of this view's message awaiting Discord, one future each,
        # from a render's first request to its last. A push or pop waits for
        # them before its own edit, so an older render cannot land last.
        self._edits_pending: Set[asyncio.Future] = set()
        # The task running a state-driven render, which takes no turn: a turn
        # taker waits for it, since that render rebuilds the tree as it goes.
        self._render_task: Optional[asyncio.Task] = None
        self._render_idle = asyncio.Event()
        self._render_idle.set()
        # The task sending or closing this view, and whether it is a send. A
        # close that finds the turn held records itself: it waits out another
        # close, and a send carries it out.
        self._lifecycle_task: Optional[asyncio.Task] = None
        self._lifecycle_sending = False
        self._lifecycle_idle = asyncio.Event()
        self._lifecycle_idle.set()
        # Closes recorded and not yet carried out: None, or (exit, how far the
        # message closes). And how far a teardown has closed the message, so
        # a later close does only what an earlier one left undone.
        self._close_request: Optional[Tuple[bool, int]] = None
        self._message_closed = _MESSAGE_LEAVE
        # The part of the request other callers left with the holder, which a
        # holder cut off by a cancel hands on rather than drops.
        self._left_close: Optional[Tuple[bool, int]] = None
        # Ids of the views attached when the current send began, which a
        # refused send releases rather than closes.
        self._attached_before_send: frozenset = frozenset()
        # Stopped by its timer rather than stop(), so its on_timeout() decides.
        self._timed_out = False
        # Turn takers between entering _reload_turn and holding the lock. A
        # render does not start while there is one.
        self._turn_claims = 0

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

        # An int or None passes, a Member or other object with an int id
        # becomes that id, and anything else raises TypeError here.
        self.user_id = coerce_snowflake_id(self.user_id)
        self.guild_id = coerce_snowflake_id(self.guild_id)

        # Recorded for _warn_if_locked_out, which runs at send once the
        # subclass __init__ has finished and allowed_users exists.
        self._explicit_user_id = _user_id_supplied

        if self.session_id is None and self.user_id is not None:
            # The class path keeps same-named classes in different modules
            # apart; each open gets its own session unless the class sets
            # session_continuity. A pushed view inherits its source's session.
            class_key = type(self)._class_session_key()
            if type(self).session_continuity:
                self.session_id = f"{class_key}:user_{self.user_id}"
            else:
                self.session_id = f"{class_key}:user_{self.user_id}:{uuid.uuid4().hex[:8]}"

        # Read through the MRO, so a base class's declaration is inherited, and
        # copied per instance. Empty means none but UNDO and REDO, which pass
        # every filter; None means all.
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
        """Create a task owned by this view.

        The task is cancelled when the view exits, times out, or hands its
        message to a ``push()`` or ``pop()`` destination. An ``exit()`` or
        ``on_timeout()`` called from the task itself finishes first, and the
        task is cancelled once the call returns; one that navigates the view
        away carries on as the destination's.
        """
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
        domain-stable value (e.g. `persistence_key=f"counter:{user_id}"`)
        whenever the slot is persistent.
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
            channel_id=self._channel_id_of(message),
        )
        await self.dispatch("VIEW_UPDATED", payload)

    async def _send_defer_timer(self, ephemeral: bool) -> None:
        """Defer a slow send's interaction before the 3s wall (the send's ack backstop).

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
        # What was carried, so the navigation's commit can bring the successor
        # up to date with steps this view takes while it loads.
        new_view._undo_carried = (
            self,
            _stack_seqs(old_view_state.get("undo_stack")),
            _stack_seqs(old_view_state.get("redo_stack")),
        )
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

    def _settle_undo_carry(self, new_view) -> None:
        """Bring a navigation's destination up to date with undo steps taken since the carry.

        The stacks are carried when the destination is built, so a change or
        an Undo on this view while the destination loads would otherwise be
        missing from the timeline, or a step already undone kept. The
        destination keeps the steps it recorded itself since.
        """
        carried, new_view._undo_carried = new_view._undo_carried, None
        if carried is None or carried[0] is not self:
            return
        _, carried_undo, carried_redo = carried
        store = self.state_store

        def write() -> None:
            views = store.state.get("views", {})
            source, destination = views.get(self.id), views.get(new_view.id)
            if source is None or destination is None:
                return
            source_undo = list(source.get("undo_stack") or ())
            source_redo = list(source.get("redo_stack") or ())
            if (
                _stack_seqs(source_undo) == carried_undo
                and _stack_seqs(source_redo) == carried_redo
            ):
                return
            own = [
                entry
                for entry in destination.get("undo_stack") or ()
                if entry.get("seq", -1) not in carried_undo
            ]
            undo = sorted(source_undo + own, key=lambda entry: entry.get("seq", -1))
            limit = new_view.undo_limit
            if limit and len(undo) > limit:
                undo = undo[-limit:]
            # A step the destination recorded itself cleared its redo history.
            redo = [] if own else source_redo
            store.state = {
                **store.state,
                "views": {
                    **views,
                    new_view.id: {**destination, "undo_stack": undo, "redo_stack": redo},
                },
            }

        store._write_when_free(write)

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
            if child._torn_down():
                # Closed while the navigation ran (an exit_children() the
                # commit cut off): dropped, as the cascade drops it.
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
        if new_view._attached_to is self:
            new_view._attached_to = None
        self._attached_children.clear()

    def _check_hand_off(self, new_view) -> None:
        """Raise when the commit could not carry this view's links to ``new_view``.

        Checked before a navigation's edit, the same links
        :meth:`_carry_attachments_to` creates, so a link that would close a
        cycle rolls the navigation back instead of raising at the commit,
        after the edit landed and with the navigation half-settled.
        """
        for child in self._attached_children:
            if child is not new_view:
                new_view._check_attachment(child._last_successor())
        parent = self._attached_to
        if parent is not None and not parent.is_finished():
            parent._last_successor()._check_attachment(new_view)

    async def _rollback_send(self) -> None:
        """Undo what a failed ``send()`` built, judged by what exists rather than by stage.

        A view already live on a message before this send (the same instance
        sent again) owns nothing the failed send created: it stays live, and
        only the per-send state the send reassigned is put back. Otherwise
        views attached to it during the send close, as its ``exit()`` would
        close them, while views attached earlier are left as they were and
        released from it. The store subscriber and undo-tracking entry
        ``__init__`` created are released, and so is a registration, whoever
        made it: a ``replace()`` destination is registered by the navigation
        before its own send.
        """
        restore = self._live_before_send
        if restore is not None:
            self._ephemeral, self._refresh_handoff_resolved, self._ephemeral_arm_deadline = restore
            if (
                self._refresh_handoff
                and self._ephemeral_arm_deadline is not None
                and not self._refresh_armed
                and not self.is_finished()
            ):
                # The send cleared the deadline, which stood its timer down.
                self._schedule_ephemeral_refresh()
            return
        # A refused or vetoed send leaves no side effects, so a view attached
        # before it began keeps its message; it outlives a parent that never
        # went live.
        for child in [c for c in self._attached_children if id(c) in self._attached_before_send]:
            self._attached_children.remove(child)
            if child._attached_to is self:
                child._attached_to = None
        try:
            # Views its on_load() sent with parent=self close with it.
            await self._cleanup_attached_children()
        finally:
            # A cancel or a raise in that cascade would otherwise leave a
            # registered view with no message, counted by its instance limit.
            self.stop()
            # Unconditional: on_load() at Stage 0a may have spawned a task even
            # on a path that never reached the registries. The calling task is
            # kept, since it receives this send's failure.
            self.task_manager._cancel_for_teardown(self.id, keep_caller=True)
            self.state_store._unsubscribe(self.id)
            self.state_store._undo_enabled_views.pop(self.id, None)
            # The attach never happened: it is the last stage of a successful
            # send. Left set, `parent` would name a view that does not track
            # this one and will not tear it down.
            self._pending_parent = None
            if self.id in self.state_store._active_views:
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
            The sent ``discord.Message``, or ``None`` when the send put no
            live view on a new message: a policy gate blocked it (a view sent
            again stays live on the message it was on), or the view was
            closed or stopped while it was being sent. One closed or stopped
            before it had loaded posts nothing; later, the close is carried
            out on the posted message, frozen or deleted as it asked, and a
            stopped view is frozen as a timeout leaves it.

        Raises:
            RuntimeError: The view has closed, is closing, or is already being
                sent, or the call comes from inside a push or pop the view
                takes part in.
            asyncio.CancelledError: The send was cancelled. Before Discord
                accepted the message, the send is rolled back; after, the
                step the cancel lands in is cut and the steps after it still
                run (registering and attaching the view, closing the message
                a re-send left) for at most five seconds, then it is raised.
                A cut close of the message a re-send left finishes on its own.
                A close requested during the send goes on after it returns.
        """
        # A push or pop hands the view's message to another view, which would
        # leave this send posting a view already torn down.
        await self._wait_out_navigation("send()")
        self._refuse_send()
        self._lifecycle_task = asyncio.current_task()
        self._lifecycle_sending = True
        self._lifecycle_idle.clear()
        # Its bound covers the close a cancelled send still carries out.
        cut = _HeldCancel()
        try:
            try:
                message = await self._run_send(send_kwargs, ephemeral=ephemeral, cut=cut)
            except BaseException:
                self._close_if_stopped()
                if cut.expired and self._close_request is not None:
                    # The hold on the cancel ran out: a close recorded during
                    # the send carries on in a task of its own, which takes
                    # the view's turn once this send lets go of it.
                    request, self._close_request = self._close_request, None
                    self._resume_close(*request)
                else:
                    await self._close_after_send(raising=True)
                raise
            self._close_if_stopped()
            await self._close_after_send()
            # A close that failed leaves the view live on the posted message.
            return None if self._torn_down() else message
        finally:
            if cut.expired:
                logger.warning(
                    f"{type(self).__name__}.send() was cancelled after Discord accepted its "
                    f"message and did not finish within {_CANCEL_HOLD_SECONDS:g}s; the steps "
                    f"still running were cut, so the view may be unregistered or unattached, "
                    f"or the message it was sent from left as it was."
                )
            cut.release()
            self._release_lifecycle_turn()
            # A send that ended without posting released no turn after a
            # render was deferred during it, so the message the view stays on
            # gets that render now.
            self._replay_if_owed()

    async def _run_send(self, send_kwargs, *, ephemeral=False, cut: _HeldCancel):
        """The body of :meth:`_send_pipeline`, run while it holds the view."""
        # Diagnostics only, and cheap: the subclass __init__ has returned by
        # now, so allowed_users exists and the reserved-user_id question can
        # finally be answered. Never blocks the send.
        self._warn_if_locked_out()

        send_defer_task = None
        proceeding = False
        self._sending_task = asyncio.current_task()
        self._render_seq += 1
        self._send_start = self._render_seq
        # A view already live on a message is being sent again. A failed send
        # leaves it that way, putting back the per-send state reassigned below.
        live = (
            self._message is not None
            and not self.is_finished()
            and self.id in self.state_store._active_views
        )
        self._live_before_send = (
            (
                self._ephemeral,
                getattr(self, "_refresh_handoff_resolved", None),
                self._ephemeral_arm_deadline,
            )
            if live
            else None
        )
        # What Stage 7 needs to close the message a successful send moves the
        # view off, taken now: what that message shows, before on_load()
        # rebuilds the tree (possibly for another viewer), and the view
        # store, which a stop() during the send detaches.
        left = (
            {
                "message": self._message,
                "ephemeral": self._ephemeral,
                "store": self._view_store(),
                "carried": self._registry_message_id,
                "shown": self._shown_components() if self._is_layout() else None,
            }
            if live
            else None
        )
        # Views attached before now outlive a send that is refused.
        self._attached_before_send = frozenset(map(id, self._attached_children))
        # A refresh handoff pending from an earlier send of this instance
        # describes that message, not the one this send creates, and its timer
        # checks the deadline it slept against. Stage 7 stamps a new one for an
        # ephemeral send.
        self._ephemeral_arm_deadline = None
        # -- Stage 0: pre-send gate --
        # on_pre_send() runs with the response slot still open, after
        # _prepare_send() (which binds a persistent view). The ack backstop
        # stands down on every exit from the pre-send stages, or it would
        # defer after send() had already returned None.
        try:
            try:
                await self._prepare_send()
                proceed = await await_maybe(self.on_pre_send(self.interaction))
            except BaseException:
                # A raising or cancelled veto hook aborts the send as a False
                # does, and owes the same cleanup before the error goes on.
                await self._rollback_send()
                raise
            if not proceed:
                await self._rollback_send()
                return None

            # The slow stages ahead (on_load, instance limit, seed) run on the
            # 3s clock. Armed after the veto, whose open response slot it would
            # otherwise take; a send from a click is covered by the click's timer.
            if (
                self.auto_defer
                and self.interaction is not None
                and not self.interaction.response.is_done()
            ):
                send_defer_task = asyncio.create_task(self._send_defer_timer(ephemeral))

            # -- Stage 0a: async preload --
            # Under the reload turn, so another task's reload cannot run a
            # second on_load() beside it. Released before registration, where
            # code (a store.on() hook, seed_initial_state) may await a task that
            # reloads this view.
            try:
                async with self._reload_turn("send()"):
                    await self._run_on_load()
                self._validate_loaded_tree()
                held = self._held_mention_rules
                if held is not None:
                    # A render held before on_load rebuilt the tree shows
                    # what the send posts.
                    self._held_mention_rules = (
                        held[0],
                        held[1],
                        self._shown_digest(),
                    )
            except BaseException:
                await self._rollback_send()
                raise
            if self._close_request is not None or self.is_finished():
                # Closed or stopped while it loaded (its own on_load() can do
                # either). Nothing is registered or posted yet, and no other
                # view has been replaced for it, so the send stops here.
                await self._rollback_send()
                return None

            # -- Stage 1: instance enforcement --
            try:
                await self._enforce_instance_limit()
            except InstanceLimitError as e:
                # In a finally: the default hook re-raises when it has neither
                # an interaction nor a context to answer on, and an override
                # may too.
                try:
                    await await_maybe(self.on_instance_limit(e))
                finally:
                    await self._rollback_send()
                return None
            except BaseException:
                # A cancellation while an older view is replaced for this one
                # (its on_replaced() or its exit()) leaves this view unsent.
                await self._rollback_send()
                raise

            # -- Stage 2+3: state registration and participant claiming --
            # One batch, so the registration (and a rollback's VIEW_DESTROYED)
            # reports as one notification; source_id renders this view inline.
            # The bot is wired first, so a close during the send releases the
            # view with the bot's others.
            bot = self._client()
            if bot is not None:
                self.state_store._install_message_cleanup(bot)
            try:
                async with self.state_store.batch(source_id=self.id):
                    try:
                        self.state_store._register_view(self)
                        await self._register_state()
                        # Seed hook fires after registration so the view exists in
                        # state, but inside the batch so any seeding dispatches join
                        # the same BATCH_COMPLETE notification. Subscribers see the
                        # seeded slot from frame one. Default is a no-op.
                        await await_maybe(self.seed_initial_state(self.state_store.state))
                        claimed = (
                            not self.auto_register_participants
                            or await self._auto_register_participants()
                        )
                    except BaseException:
                        # A raise or cancel here (a seed hook, a limit hook
                        # override) would leave a registered view with no
                        # message, locking its owner out under "reject". The
                        # rollback's dispatches join this batch.
                        await self._rollback_send()
                        raise
                    if not claimed:
                        await self._rollback_send()
                        return None
            except BaseException:
                # A cancel in the batch's exit (a view_created hook awaiting)
                # leaves the same registered view. Running again after the
                # inner rollback lands a teardown a middleware refused once.
                await self._rollback_send()
                raise

            # -- Stage 4: ephemeral refresh-handoff resolution --
            # auto_refresh_ephemeral=None derives from timeout at each
            # ephemeral send (900s is the token's life); the declaration itself
            # is never assigned. _ephemeral follows every send, so an instance
            # sent again publicly does not keep a stale True.
            self._ephemeral = ephemeral
            if ephemeral and self.auto_refresh_ephemeral is None:
                self._refresh_handoff_resolved = self.timeout is None or self.timeout > 900
            proceeding = True
        finally:
            # A send that proceeds keeps the backstop through the wait for the
            # delivery turn; _deliver_send stands it down just before posting.
            if not proceeding:
                self._stand_down(send_defer_task)
                self._stop_holding()

        # -- Stage 5 + 6: Discord send and message re-fetch --
        # Under the reload turn, so another task's reload cannot rebuild the
        # tree while it is serialized. Rollbacks release the turn first, since
        # their hooks may reload this view. A cancel once the message is posted
        # is held in ``cut`` until the rest of the send has run.
        turn = contextlib.AsyncExitStack()
        try:
            await turn.enter_async_context(self._reload_turn("send()"))
            # After seed_initial_state, so a V1 pattern's first embed reads the
            # seeded state.
            await self._preload_send_content(send_kwargs)
        except BaseException:
            await turn.aclose()
            self._stand_down(send_defer_task)
            self._stop_holding()
            await self._rollback_send()
            raise
        async with turn:
            try:
                shipped_digest = await self._deliver_send(
                    send_kwargs, ephemeral, send_defer_task, turn, cut=cut
                )
            finally:
                self._stand_down(send_defer_task)
                # Cleared before the turn is released, so the release replays
                # a render deferred during the send against the new message.
                self._stop_holding()

        # Everything below is bookkeeping against a message Discord has
        # already accepted, so a failure must not surface as a failed send:
        # a caller that retries on that answer posts a second copy. Each step
        # logs its own failure and the next still runs.
        try:
            # The render-hash baseline: the tree as it was serialized for the
            # send. For a V1 send with embed kwargs the embed is outside the
            # digest, which refresh() only trusts when it is passed no kwargs.
            self._has_rendered = True
            self._last_tree_digest = shipped_digest

            await self._post_send_step(
                "recording its message in the state store",
                lambda: self._update_message_state(self._message),
                cut,
            )

            # -- Stage 7: ephemeral refresh + parent attach --
            if ephemeral:
                # Stamped for every ephemeral send, since the deadline is a
                # fact about the message's token clock, not about whether
                # this view acts on it -- the timer below runs only when the
                # effective handoff policy engaged.
                self._ephemeral_arm_deadline = time.monotonic() + max(
                    1, 900 - self.refresh_warning_seconds
                )
                # A view stopped meanwhile (its bot closed, or a stop() it is
                # frozen for) has no panel to hand on.
                if self._refresh_handoff and not self.is_finished():
                    await self._post_send_step(
                        "starting the ephemeral refresh timer",
                        self._schedule_ephemeral_refresh,
                        cut,
                    )

            if self._pending_parent is not None:
                parent = self._pending_parent._last_successor()
                self._pending_parent = None
                if parent._closed() or parent._torn_down():
                    # The parent closed while this view was being sent, and
                    # its cleanup, which closes its children, has run.
                    await self._post_send_step(
                        "closing it after its parent closed",
                        lambda: self.exit(delete_message=True),
                        cut,
                    )
                else:
                    await self._post_send_step(
                        "attaching it to its parent",
                        lambda: parent.attach_child(self),
                        cut,
                    )

            # No message: a render during the send found the new one deleted.
            if self._message is not None and self._close_request is None and not self.is_finished():
                await self._post_send_step(
                    "finishing the send", lambda: self._after_send(self._message), cut
                )
                if left is not None:
                    await self._post_send_step(
                        "moving the registrations it carries",
                        lambda: self._move_carried_registrations(left["carried"], self._message),
                        cut,
                    )

            # After the registration, so closing the message left cannot hold
            # it up: a persistent panel's row moves to the new message first.
            if left is not None:
                await self._post_send_step(
                    "closing the message it left", lambda: self._leave_message(**left), cut
                )
        finally:
            if left is not None:
                # Whatever cut the steps above short, the message the view
                # left stops acting for it.
                self._let_go_of_message(left["message"], left["store"])

        if cut:
            # The message is live and the rest of the send has run: the cancel
            # that landed after the post goes through now.
            raise asyncio.CancelledError()
        return self._message

    async def _prepare_send(self) -> None:
        """Get the view ready to send, first thing inside the send.

        Runs while the send holds the view, so a close meanwhile is carried
        out by the send. The default does nothing; a persistent view binds its
        runtime dependencies here.
        """

    async def _post_send_step(self, what: str, run: Callable[[], Any], cut: _HeldCancel) -> None:
        """Run one step of a send whose message Discord has accepted, logging a failure.

        The message is live, so a raise would report a send that happened,
        and one failed step does not skip the rest: a middleware raising on
        the message's state row would otherwise leave a re-sent panel's
        registration on the message the send then leaves open.
        """
        try:
            result = run()
            if inspect.isawaitable(result):
                await _hold_cancel(result, cut)
        except Exception as e:
            logger.error(
                f"{type(self).__name__} was sent, but {what} raised "
                f"{type(e).__name__}: {e}. The message is posted, and the send goes "
                f"on with its other steps.",
                exc_info=e,
            )

    async def _after_send(self, message) -> None:
        """Finish a send that posted ``message`` and was not closed or stopped meanwhile.

        Runs before the send lets go of the view, so a close requested
        meanwhile is carried out after it. The default does nothing; a
        persistent view registers itself here.
        """

    def _refuse_send(self) -> None:
        """Raise when this view cannot be sent: it has closed, or is being sent."""
        name = type(self).__name__
        if self._lifecycle_sending:
            raise RuntimeError(
                f"{name}.send() was called while the view is already being sent. "
                f"Fix: await the first send() before sending the view again."
            )
        if self._successor is not None or self._discarded:
            raise RuntimeError(
                f"{name}.send() was called on a view that handed its panel on (a push() "
                f"or pop() from it, an ephemeral panel's Continue button, or a restart "
                f"that restored the panel), or that a "
                f"failed push() or pop() returned. The panel belongs to another view. "
                f"Fix: construct a new view."
            )
        if self._closed():
            raise RuntimeError(
                f"{name}.send() was called on a view that has closed (exit(), a "
                f"timeout, stop(), or a send that failed or was refused), or whose "
                f"exit(), timeout, or replace() has begun. Its buttons would answer "
                f"nothing. Fix: construct a new view."
            )

    async def _send_finished(self) -> None:
        """Wait until a send of this view running in another task has finished."""
        current = asyncio.current_task()
        while self._lifecycle_sending and self._lifecycle_task is not current:
            await self._lifecycle_idle.wait()

    def _close_if_stopped(self) -> None:
        """Record a close for a view ``stop()`` stopped while it was being sent.

        discord.py routes no clicks to a stopped view, so it closes as
        ``exit()`` would rather than stay registered on screen: its message
        closed, and a registration it holds retired, since a restart would
        otherwise restore a panel nobody can use. One its timer stopped is
        left to its own ``on_timeout()``, which closes it unless an override
        says otherwise. The close is not the send's own, so a send cancelled
        while carrying it out hands it on.
        """
        if self.is_finished() and not self._torn_down() and not self._timed_out:
            self._request_close(exit=True, message=_MESSAGE_FREEZE)
            self._leave_close(exit=True, message=_MESSAGE_FREEZE)

    async def _close_after_send(self, *, raising: bool = False) -> None:
        """Carry out a close recorded while this view was being sent.

        The send holds the view, so a close from any task left its request
        here rather than run beside the send. A view the send rolled back
        is torn down already; one it posted is closed now, as the request
        asked. A failure is logged rather than raised: with ``raising`` set
        the send's own error is the one that goes on, and otherwise the
        message is posted and a raise would report a send that did happen.
        """
        if self._close_request is None:
            return
        try:
            with _teardown_scope():
                await self._carry_out_close()
        except Exception as e:
            if raising:
                outcome = "The send failed as well, and its error is the one raised."
            elif self._torn_down():
                outcome = "The view is torn down."
            else:
                outcome = "The view stays live on its posted message."
            logger.error(
                f"{type(self).__name__} was closed while it was being sent, and "
                f"closing it raised {type(e).__name__}: {e}. {outcome}",
                exc_info=e,
            )

    @staticmethod
    def _stand_down(timer: Optional[asyncio.Task]) -> None:
        """Cancel a send-scoped ack backstop that has not fired yet."""
        if timer is not None and not timer.done():
            timer.cancel()

    async def _deliver_send(
        self,
        send_kwargs,
        ephemeral,
        ack_timer: Optional[asyncio.Task],
        turn: contextlib.AsyncExitStack,
        *,
        cut: _HeldCancel,
    ) -> Optional[int]:
        """Stages 5 and 6 of :meth:`_send_pipeline`: validate, send, re-fetch.

        Returns the digest of the tree as it was serialized, which becomes
        the render baseline. Rolls the send back and re-raises on failure,
        releasing ``turn`` (the reload turn the send holds) before the
        rollback. ``ack_timer`` is the send-scoped ack backstop, stood down
        immediately before the HTTP call with no await between them. Once the
        message is posted, a cancel is held in ``cut`` for the caller to
        raise after the rest of the send, not raised here.
        """
        # -- Stage 5: Discord send --
        # The files this send carries, closed by the rollback when discord.py
        # refuses the payload before its own ``finally`` would. Bound before
        # the try, whose handler reads it; filled inside it, since filling can
        # raise (one File passed as files=) and the rollback must still run.
        files_to_close: list = []

        class_name = type(self).__name__
        try:
            # A file an earlier call sent was left at its end.
            _rewind_files(send_kwargs)
            if send_kwargs.get("file") is not None:
                files_to_close.append(send_kwargs["file"])
            if send_kwargs.get("files"):
                files_to_close.extend(send_kwargs["files"])
            # Checked on the final tree. Registration has run by now, so a
            # rejection rolls it back. Ids first, as refresh() does: a tree
            # built in on_load() or __init__ still has discord.py's random ids,
            # and the first refresh would otherwise rename them in an edit of
            # its own.
            self._stabilize_custom_ids()
            self._apply_theme_defaults()
            self._sync_back_buttons()
            self._check_placement()
            self._warn_unmatched_attachment_refs(send_kwargs)
            # The parent attach itself lands after the send, where a raise
            # would report a failure for a message Discord already has. The
            # chain is knowable now, so it is judged now.
            if self._pending_parent is not None:
                # Checked against the view attach_child() will attach to.
                self._pending_parent._last_successor()._check_attachment(self)

            # The baseline certifies the tree as serialized here. A digest
            # taken after the send would certify whatever the tree became
            # while the request was in flight.
            shipped_digest = self._shipped_digest()
            # Renders asked for until now are in the tree the post carries. The
            # post takes a number of its own, after theirs: a render that shares
            # a number with the one that set the mention rules is a later
            # refresh of the same late render, and replaces them.
            self._render_seq += 1
            posted_at = self._render_seq
            posted_shows = self._shown_digest()
            self._stand_down(ack_timer)

            if self.context and hasattr(self.context, "send"):
                if ephemeral:
                    send_kwargs["ephemeral"] = ephemeral
                message = await self.context.send(**send_kwargs)

            elif self.interaction:
                send_kwargs["ephemeral"] = ephemeral
                if not self.interaction.response.is_done():
                    try:
                        response = await self.interaction.response.send_message(**send_kwargs)
                    except discord.HTTPException as e:
                        # 40060: a cancelled send-scoped defer landed
                        # server-side in the narrow ack-window race, acking the
                        # slot after the is_done() check returned False. Ship via
                        # followup instead of rolling the send back.
                        if getattr(e, "code", None) != 40060:
                            raise
                        # The refused request streamed any file it carried.
                        _rewind_files(send_kwargs)
                        message = await self.interaction.followup.send(**send_kwargs, wait=True)
                    else:
                        # The response carries the posted message, the one
                        # original_response() would read back.
                        message = getattr(response, "resource", None)
                        if not isinstance(message, discord.InteractionMessage):
                            # Discord sent none: read it back. Posted, so a
                            # cancel there is held rather than rolling back.
                            message = None
                            while message is None:
                                message = await _hold_cancel(
                                    self.interaction.original_response(), cut
                                )
                else:
                    message = await self.interaction.followup.send(**send_kwargs, wait=True)

            else:
                raise RuntimeError(
                    f"{class_name}.send() requires either 'context' or 'interaction' to be set."
                )
        except BaseException:
            # BaseException: a send cancelled while its request is in flight
            # owes the rollback as surely as one that failed.
            for f in files_to_close:
                # Defensive double-close: discord.py closes attachments in
                # its own finally when the HTTP layer is reached. Calling
                # close() on an already-closed File is a no-op, so this
                # only changes behavior on the pre-HTTP failure path.
                try:
                    f.close()
                except Exception:
                    pass
            await turn.aclose()
            await self._rollback_send()
            raise

        # Discord has the message: it replaces every render asked for before
        # the send began. One asked for since is newer than the send's own
        # keywords, which were fixed when it was called.
        start = self._send_start
        self._superseded_at = {part: max(mark, start) for part, mark in self._superseded_at.items()}
        self._mention_rules = (send_kwargs.get("allowed_mentions"), posted_at, posted_shows)

        # -- Stage 6: message re-fetch for token-free editing --
        # A channel discord.py cannot resolve has nothing to fetch through; the
        # view keeps the message it has.
        if (
            not ephemeral
            and isinstance(message, (discord.InteractionMessage, discord.WebhookMessage))
            and message.channel is not None
        ):
            self._webhook_message = message
            try:
                self._message = await message.channel.fetch_message(message.id)
            except (*DISCORD_CALL_ERRORS, asyncio.TimeoutError):
                # The send already succeeded: the view keeps the message it has.
                # aiohttp's total timeout raises a bare TimeoutError.
                self._message = message
            except asyncio.CancelledError:
                # Cancelled after Discord accepted the send: the view keeps the
                # message it has, and the send raises the cancel once the rest
                # of it has run.
                self._message = message
                cut.hold()
            except BaseException:
                self._message = message
                raise
        else:
            self._message = message

        return shipped_digest

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
        # A message posted in a channel discord.py cannot build carries none.
        channel = getattr(self._message, "channel", None)
        if self.replaced_message and self._participants and channel is not None:
            try:
                await channel.send(self.replaced_message)
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

        - ``error`` is an ``Exception``: :meth:`build_reopen_view` raised.
          Sends ``reopen_failure_message`` as an ephemeral.
        - ``error`` is ``None``: :meth:`build_reopen_view` returned
          ``None``, signaling the session has ended. Runs
          :meth:`on_session_ended` and calls ``exit()``.

        Override for custom recovery, logging, or localized messages.
        """
        if error is None:
            await self._call_hook_safe(self.on_session_ended, interaction)
            await self.exit()
            return
        try:
            if self.reopen_failure_message is not None:
                await self.respond(interaction, self.reopen_failure_message, ephemeral=True)
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not send reopen failure response in {self.__class__.__name__}: {describe_discord_error(e)}"
            )

    async def on_session_ended(self, interaction: Interaction) -> None:
        """Called when a user reaches this view's session after it ended.

        That is a modal submitted after the view closed, or the ephemeral
        refresh button when :meth:`build_reopen_view` returned ``None``. The
        default sends ``session_ended_message`` as an ephemeral, or nothing
        when it is ``None``. Override for a localized reply, an embed, or
        logging. May be ``async def`` or plain ``def``; a raise is logged,
        and the interaction is still acknowledged.
        """
        if self.session_ended_message is None:
            return
        try:
            await self.respond(interaction, self.session_ended_message, ephemeral=True)
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Could not tell the user {type(self).__name__} had closed: "
                f"{describe_discord_error(e)}"
            )

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
        # Closed before reporting, or a "never awaited" warning reads as a
        # second fault. The theme wrapper may hold a second coroutine (from a
        # build_ui that returns one), found by shape in its frame, not by name.
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

        This method runs at the ``__init_subclass__`` wrapper around
        ``build_ui`` (covers views whose rebuild routes through
        ``build_ui``), and at every render seam: the send, a push or pop
        destination's edit, and the top of :meth:`refresh`. The seams cover
        trees built elsewhere -- in ``on_load`` or ``__init__``, and the
        pattern rebuild paths that bypass ``build_ui`` (tab switches,
        paginated page flips, wizard/form step advances, menu category
        changes).

        Each auto-generated id is rewritten to a deterministic anchor built
        from the item's label and a number standing for what a click on it
        would do (see :meth:`_custom_id_token`):

        - **Unique content** gets a content-only id, which stays put when
          conditional rendering shifts the item's tree position (an alert
          row appearing above the action row).
        - **Colliding content** (items that look the same and would do the
          same thing, like the empty cells of a board whose callback reads
          the clicked cell) adds tree coordinates. A cell whose label
          changes gets a new id; its neighbors keep theirs.

        A click sent from the previous render therefore reaches a button
        only when that button looks the same and would do the same thing.
        When a list shifts or a page turns, a row's button acts on a
        different target, so its id changes and the stale click is
        dropped instead of acting on the row that took its place.

        Items with ``_provided_custom_id = True`` (user passed
        ``custom_id=`` explicitly) are skipped -- the escape hatch wins.
        The exception is an id a builder composed from a position
        (``choice_row``, and ``button_row`` / ``tab_nav`` given a base):
        outside a persistent view it is derived like an auto-generated one.
        """
        prefix = self.id[:8]

        # (item, content key, top-level index, walk position) for every
        # interactive item with a generated custom_id.
        entries: list[tuple[Any, str, int, int]] = []
        # Ids already on the tree: a caller's own, and those an earlier pass
        # assigned before more items were added (in on_load, after build_ui).
        taken: set = set()
        for container_idx, top in enumerate(self._children):
            if hasattr(top, "walk_children"):
                inner = list(top.walk_children())
            else:
                inner = [top]
            for pos, item in enumerate(inner):
                # Only Buttons and Selects carry a real custom_id; every Item
                # sets _provided_custom_id, so a hasattr test would stamp
                # display items too.
                if not isinstance(item, (Button, BaseSelect)):
                    continue
                # A builder's composed id names a position; outside a
                # persistent view, whose ids must survive a restart, it is
                # derived like an auto-generated one.
                if item._provided_custom_id and (
                    self._persistent or not getattr(item, "_cascadeui_composed_id", False)
                ):
                    taken.add(item.custom_id)
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
                content_key = f"{callback_name}#{self._custom_id_token(item)}:{label}"
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
            anchor = self._fit_custom_id(f"{prefix}:{ck}")
            if key_counts[ck] > 1 or anchor in taken:
                anchor = self._fit_custom_id(f"{prefix}:{ck}@{c_idx}.{p_idx}")
            item.custom_id = anchor
            # Assigning sets discord.py's _provided_custom_id, so a persistent
            # view's missing-id check reads this mark instead. The ids anchor on
            # self.id: stable across rebuilds, not across a restart.
            item._cascadeui_stabilized = True
            if trace_on:
                assigned.append(f"{type(item).__name__}({id(item):x})={item.custom_id}")
        if assigned:
            logger.debug(
                f"[viewstore-trace] _stabilize_custom_ids view={self.id[:8]} "
                f"cls={type(self).__name__} :: " + "; ".join(assigned)
            )

    def _custom_id_token(self, item) -> int:
        """The number standing for what a click on ``item`` would do.

        Items with equal signatures (:func:`item_signature`) get the same
        number, in this render and later ones, so an id survives a rebuild
        exactly when the button would still do the same thing. A signature
        seen for the first time takes the next unused number; numbers are
        never reused, so a button that returns after dropping out of the
        remembered history gets a new id rather than an old one.
        """
        tokens = self._custom_id_tokens
        signature = item_signature(item)
        token = tokens.get(signature)
        if token is not None:
            tokens.move_to_end(signature)
            return token
        token = tokens[signature] = self._custom_id_token_next
        self._custom_id_token_next += 1
        if len(tokens) > _CUSTOM_ID_TOKEN_HISTORY:
            tokens.popitem(last=False)
        return token

    async def _edit_and_digest(self, edit) -> Optional[int]:
        """Digest the tree, then start ``edit()`` in the same step; return the digest.

        An edit serializes the tree synchronously when its coroutine first
        runs. Bounded before Python 3.12, which runs the edit as a task, that
        first step lands on a later loop iteration, and a digest taken before the
        wrap could certify a tree other work had since rebuilt. Running
        both inside the wrapped coroutine keeps them in one step on every
        interpreter. Callers bind the message the edit targets before the
        wrap (``functools.partial``): read inside it, the attribute could
        already be ``None`` from a concurrent refresh that found it deleted.
        """
        digest = self._shipped_digest()
        await edit()
        return digest

    def _release_render(self, rendering: Optional[asyncio.Future]) -> None:
        """Stop counting a render as in flight, once its last request is done."""
        if rendering is not None and not rendering.done():
            self._edits_pending.discard(rendering)
            rendering.set_result(None)

    def _shipped_digest(self) -> Optional[int]:
        """Digest the tree an edit is about to serialize, or ``None`` if it raises.

        Taken before the edit's await, so the baseline certifies what was
        sent rather than whatever the tree became while the request was in
        flight. A digest that raises (a user item's property read) must not
        stop the edit, so it logs and returns ``None``, and the next refresh
        ships unconditionally.
        """
        try:
            return self._compute_tree_digest()
        except Exception as e:
            logger.error(
                f"{type(self).__name__} could not digest its component tree "
                f"({type(e).__name__}: {e}); the edit ships without a render "
                f"baseline, so the next refresh ships unconditionally.",
                exc_info=e,
            )
            return None

    def _shown_digest(self) -> Optional[int]:
        """What the tree shows, ids left out, or ``None`` if it cannot be read.

        For the V2 mention rules. :meth:`_shipped_digest` logs a tree that
        cannot be digested when the edit ships; read as ``None`` here, it
        compares as changed.
        """
        try:
            return self._compute_tree_digest(ignore_ids=True)
        except Exception:
            return None

    def _compute_tree_digest(
        self, *, ignore_disabled: bool = False, ignore_ids: bool = False
    ) -> int:
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
        Anything else (internal python ids, callback identity, ephemeral
        view back-references) is deliberately excluded. Two views that
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

        ``ignore_disabled=True`` leaves ``disabled`` out, for the one caller
        asking whether content changed rather than whether a render is
        owed: the restore baseline in :meth:`_teardown_edit_target`, where a
        freeze is not new content.

        ``ignore_ids=True`` leaves component and custom ids out, for the V2
        mention rules, which follow what the message shows: ids are
        normalized only when a render ships, so a tree compared before that
        would otherwise differ from the same tree compared after.
        """
        parts: list = []
        for item in self.walk_children():
            if isinstance(item, DynamicItem):
                # A dynamic button serializes as the item it wraps, which is
                # where its label, style, and disabled state live.
                item = item.item
            # ``id`` is wire-visible on every component type, so it rides the
            # walk rather than each per-type branch. Without it a rebuild that
            # renumbers nodes and changes nothing else hashes identically and
            # refresh() skips the edit, leaving Discord holding the old ids.
            component_id = None if ignore_ids else getattr(item, "id", None)
            if component_id is not None:
                parts.append(("i", component_id))
            # Typed branches come before the ``custom_id`` catch-all: which
            # classes carry one is discord.py's to change, and a display item
            # that gained one would otherwise be hashed as a button.
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
            # A Separator's spacing and visibility are on the wire, so adding,
            # removing, or resizing one changes what renders.
            elif isinstance(item, Separator):
                parts.append(("s", getattr(item.spacing, "value", item.spacing), item.visible))
            # Buttons and selects: record the wire-visible attributes.
            elif hasattr(item, "custom_id"):
                # A selection lives in opt.default, which set_selected()
                # changes. Only string selects expose options.
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
                        None if ignore_ids else getattr(item, "custom_id", None),
                        getattr(item, "label", None),
                        # ButtonStyle is an IntEnum; its value is what ships.
                        getattr(getattr(item, "style", None), "value", None),
                        None if ignore_disabled else getattr(item, "disabled", None),
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
        return self._disable_items(self)

    @staticmethod
    def _disable_items(view) -> int:
        """Disable every interactive item in ``view``; return how many changed.

        Link buttons stay enabled: a link opens its URL with no view behind
        it, so it still works on a closed panel.
        """
        items = view.walk_children() if view._is_layout() else view.children
        frozen = 0
        for item in items:
            if isinstance(item, DynamicItem):
                # Its disabled state is the wrapped item's; the wrapper has none.
                item = item.item
            if isinstance(item, Button) and item.url is not None:
                continue
            if hasattr(item, "disabled") and not item.disabled:
                item.disabled = True
                frozen += 1
        return frozen

    def _teardown_edit_target(self):
        """Pick what a teardown freeze edit ships: a view, or ``None`` to skip.

        Owns the freeze so each branch reads the tree at the right moment:
        the restore check compares content before anything is disabled, and
        step 4 compares the frozen tree against the last render.

        1. A view restored from persistence that has not rendered and whose
           content still matches ``_reattach_baseline_digest`` did not put
           its own tree on screen. The message shows the render from before
           the restart, so that is what gets frozen (see
           :meth:`_frozen_on_screen_view`).
        2. Otherwise the view's own tree is the candidate, frozen. A tree
           Discord would reject never ships: an empty V2 tree is error
           50006 whatever produced it, and anything ``_check_placement``
           refuses is a 400. The other render seams raise on these;
           teardown has already destroyed the view's state, so it logs
           instead. The empty check runs even when placement validation is
           turned off. A restored view that never rendered still has the
           pre-restart panel on screen, so it freezes that rather than
           leaving live-looking controls up; any other view skips.
        3. A freeze that disabled something ships.
        4. A view that has rendered ships when its tree differs from the
           last render. ``_has_rendered`` is the gate, not
           ``_last_tree_digest is not None``: the digest is also ``None``
           after a deliberate drop (a stalled edit, a transport failure).
        5. Any other view ships: a restored view whose teardown override
           composed new content, or a view bound to a message without
           rendering (the ``message`` setter), whose farewell card has to
           reach the screen.
        """
        restored_unrendered = self._restored_unrendered()
        if (
            restored_unrendered
            and self._compute_tree_digest(ignore_disabled=True) == self._reattach_baseline_digest
        ):
            self._freeze_components()
            return self._frozen_on_screen_view()

        name = type(self).__name__
        froze = self._freeze_components()
        if self._is_layout() and not self.children:
            rejected = "an empty component tree"
        else:
            try:
                self._check_placement()
                rejected = None
            except ValueError as e:
                rejected = f"a tree Discord would reject ({e})"
        if rejected is not None:
            if restored_unrendered:
                logger.warning(
                    f"{name} was torn down with {rejected}; freezing the panel "
                    f"already on screen instead."
                )
                return self._frozen_on_screen_view()
            if self._has_rendered or self.children:
                logger.warning(
                    f"{name} was torn down with {rejected}; the freeze edit was "
                    f"skipped, so the message keeps its last render."
                )
            else:
                logger.debug(f"{name} was torn down before rendering a tree; no edit sent.")
            return None
        if froze:
            return self
        if self._has_rendered:
            return self if self._compute_tree_digest() != self._last_tree_digest else None
        return self

    def _restored_unrendered(self) -> bool:
        """Whether the view was restored from persistence and has not rendered since.

        Its message then shows the render from before the restart, not the
        view's own tree.
        """
        return not self._has_rendered and self._reattach_baseline_digest is not None

    def _shown_components(self) -> list:
        """The components the view's message shows, to freeze it from later."""
        if self._restored_unrendered():
            return list(getattr(self._message, "components", None) or ())
        return [c for c in map(_component_factory, self.to_components()) if c is not None]

    def _frozen_on_screen_view(self):
        """The view's message as it stands, rebuilt and disabled, or ``None``.

        Used when this view never put its own tree on screen, so freezing
        ``self`` would overwrite the real panel. See :meth:`_frozen_copy_of`.
        """
        return self._frozen_copy_of(getattr(self._message, "components", None))

    def _frozen_copy_of(self, components):
        """Message ``components`` rebuilt into a stopped, disabled view, or ``None``.

        Three things keep the copy shippable:

        - Uploaded media (an item carrying ``attachment_id``) is re-pointed at
          ``attachment://<filename>``. A fetched message resolves it to a
          signed CDN URL, and discord.py serializes only that URL: a File
          component refuses it, and a gallery or thumbnail image breaks
          once the signature expires. The filename comes from that URL's
          path, because a components message lists no attachments of its
          own; Discord binds the reference back to the existing upload.
        - The copy runs placement validation unless the class sets
          ``validate_placement = False``, the same opt-out its own tree
          honors. discord.py drops component types it does not recognize
          while rebuilding, which can leave a container empty. A copy with
          nothing interactive left needs no check: it has nothing to freeze,
          so no edit ships.
        - The copy is stopped, which keeps ``Message.edit`` from registering
          it with the view store.

        Rebuilding reads Discord's data rather than the view's own tree, so any failure
        skips the cosmetic edit instead of breaking a teardown whose state
        work has already run. It logs a warning, because the panel is then
        left up with controls that look live and no longer answer.
        """
        try:
            # from_message reads only the components.
            copy = type(self).from_message(SimpleNamespace(components=components), timeout=None)

            def pinned(media):
                if getattr(media, "attachment_id", None) is None:
                    return media
                return f"attachment://{unquote(urlsplit(media.url).path.rsplit('/', 1)[-1])}"

            for item in copy.walk_children():
                if isinstance(item, (Thumbnail, UIFile)):
                    item.media = pinned(item.media)
                elif isinstance(item, MediaGallery):
                    for entry in item.items:
                        entry.media = pinned(entry.media)
            if copy._is_layout() and getattr(self, "validate_placement", False):
                from ._placement import validate_placement

                validate_placement(copy)
        except Exception as e:
            logger.warning(
                f"{type(self).__name__} could not freeze the panel on its message "
                f"({type(e).__name__}: {e}); no edit sent, so the panel stays unfrozen."
            )
            return None
        if not self._disable_items(copy):
            return None
        copy.stop()
        return copy

    def _dispatch_timeout(self):
        """Repair the dynamic-item registry at the actual discord.py choke point.

        discord.py's internal timer calls this method directly (not
        ``stop()``) to invoke the cancel callback and then schedule
        ``on_timeout()`` as a separate task. ``on_timeout()`` is a
        documented override point ("Override this method to delete the
        message on timeout instead"), so a subclass overriding it without
        calling ``super()`` would silently skip a repair placed there.
        Overriding this method instead runs the repair unconditionally,
        synchronously, right after ``super()._dispatch_timeout()`` returns
        -- after discord.py's own pop and after ``on_timeout`` has been
        scheduled as a task, but before that task gets a chance to run,
        regardless of what any override of the public hook does.

        A view pushed onto a persistent panel's message returns to the panel
        instead of timing out, since the message is the panel's and it never
        times out: frozen, it would stay dead for everyone until a restart. The
        return runs here, before discord.py stops the view, because a stopped
        view cannot navigate.
        """
        depth = self._panel_to_return_to()
        if depth is not None:
            self.task_manager.create_task(self.id, self._return_to_panel(depth))
            return
        self._time_out()

    def _time_out(self) -> None:
        """Time the view out: stop it and schedule ``on_timeout()``."""
        self._timed_out = True
        super()._dispatch_timeout()
        self._redrive_dynamic_items()

    def _panel_to_return_to(self) -> Optional[int]:
        """The navigation depth of the persistent panel an idle timeout returns to, or ``None``.

        A view carrying a panel's registration was pushed onto that panel's
        message, and the panel's own entry is in its navigation stack. A view
        that defines its own ``on_timeout()`` times out as it says instead.
        """
        carried = self._registry_message_id
        if carried is None or not self._nav_stack:
            return None
        if getattr(self.on_timeout, "__func__", None) is not _StatefulMixin.on_timeout:
            return None
        keys = set(self.state_store._persistence_keys_for_message(carried))
        for depth in range(len(self._nav_stack) - 1, -1, -1):
            kwargs = self._nav_stack[depth].get("kwargs") or {}
            if kwargs.get("persistence_key") in keys:
                return depth
        return None

    async def _return_to_panel(self, depth: int) -> None:
        """Navigate back to the panel at ``depth``; time out as usual if that fails.

        A view that navigated away while this waited has stopped. One an
        ``exit()`` began closing times out as it would have without the
        return, so an exit cut off afterwards does not leave it live with its
        timer already spent.
        """
        try:
            await self._wait_to_navigate("A timeout")
            if not self._closed():
                await self._pop_to(depth, None, None)
        except Exception as e:
            logger.warning(
                f"{type(self).__name__} could not return to its panel on timeout "
                f"({type(e).__name__}: {e}); timing it out instead.",
                exc_info=e,
            )
        if not self.is_finished():
            self._time_out()

    @_in_teardown_scope
    async def on_timeout(self) -> None:
        """Called when the view times out. Disables all components and cleans up state.

        Freezing is unconditional here: ``exit_policy`` governs close
        gestures, and an expiry is not one. Override this method to
        delete the message on timeout instead. A timeout that fires while
        an ``exit()`` is closing the view waits for it and adds nothing.
        Called directly, it refuses clicks and navigation at once and stops
        the view once its attached views have closed, as the timer would.
        """
        # A push, a pop, or a Continue from this view may be about to hand
        # its message over.
        await self._wait_out_navigation("on_timeout()")
        await self._wait_out_reopen("on_timeout()")
        await self._close(exit=False, message=_MESSAGE_FREEZE)

    async def _timeout_body(self) -> None:
        """Tear the view down for ``on_timeout()``: freeze, keep the registration."""
        # Closing, as an exit() is, so the view takes no clicks or navigation
        # while its children close, and one cut off before it stops is live
        # again rather than stopped.
        self._closing = True

        # Exit tracked child views first
        await self._cleanup_attached_children()

        # Before the freeze edit, as in exit(), so the instance slot is free
        # while that edit runs.
        self.task_manager._cancel_for_teardown(self.id, spare=self._deliveries)
        # discord.py stops a view before its timer calls on_timeout(); a direct
        # call has not, and discord.py would keep routing the view's clicks.
        if not self.is_finished():
            self.stop()
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        await self.state_store._destroy_view(self.id, source_id=self.id)
        await self._freeze_for_timeout()

    async def _freeze_for_timeout(self) -> None:
        """Freeze the message as a timeout leaves it: every component disabled."""
        self._message_closed = max(self._message_closed, _MESSAGE_FREEZE)
        # A render still in flight would land after the freeze.
        await self._wait_for_own_edits("Timing out")
        # _teardown_edit_target owns the freeze and decides what, if anything, ships.
        content, redrawn = await self._reclaim_content() if self._message else ({}, 0)
        target = self._teardown_edit_target() if self._message else None
        if target is not None:
            try:
                await self._ship_freeze(target, content, redrawn)
            except discord.NotFound:
                pass  # Message was already deleted
            except asyncio.TimeoutError as e:
                if isinstance(e, aiohttp.ClientError):
                    # aiohttp's connect and socket timeouts are TimeoutErrors
                    # too: a network failure, not a stall past edit_timeout.
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
                # An ephemeral's edit fails once its 15-minute token is gone,
                # so any failure on an ephemeral is DEBUG; on any other view
                # it is a WARNING.
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

        Triggered when the message this view is attached to is removed: by
        the gateway's message-delete events (a delete or a bulk purge, which
        Discord reports only to a bot holding the message intents), by the
        deletion of the channel or thread holding it, or by an edit that
        finds the message already gone, after ``on_message_gone`` has run. The
        default implementation closes the view with ``exit(delete_message=False)``
        since the message is already gone, and when a push or pop from it lands
        while that exit waits, closes the view it handed the message to as well.
        A view already torn down is not called again.

        Override for custom behavior (logging, re-sending to a new
        message, notifying the owner). If you override without calling
        ``exit()``, the view stays registered as a ghost in the state
        store -- call ``super().on_message_delete()`` or ``exit()``
        explicitly to clean up.
        """
        # The message is already gone -- null the reference so exit()
        # skips the edit/delete block entirely (no stale NotFound error).
        self._message = None
        await self._exit_or_successor(delete_message=False)

    async def on_message_gone(self) -> None:
        """Called when an edit observes the view's message is already gone.

        ``refresh()`` issues the edit that fails with ``discord.NotFound``
        when the message was deleted out from under the view; a message the
        view's own close deleted does not call it. The library
        nulls ``self._message`` and calls this hook so a consumer that records
        the message elsewhere (its own database row, an external index) can
        reconcile that reference. Key the reconciliation on the view's stable
        identity, such as ``persistence_key``, since ``self._message`` is
        already nulled.

        Default is a no-op. The hook runs on a task of its own once the render
        that found the message gone has returned, so an override may dispatch,
        reload, or send the view again. It is not called when the view is on
        a message again by then. Once it returns, the library tears the view
        down through ``on_message_delete``, the same path the gateway's
        deletion events take, unless the override sent the view again, so an
        override never needs to exit and a bot without message intents still
        retires the view. The ``REGISTRY_PRUNED``
        action is the restart-time counterpart, fired when reattach finds a
        persistent view's message gone. Make the reconciliation idempotent,
        since a deletion the gateway also reports can reach a consumer twice.
        """
        return

    def _schedule_message_gone_teardown(self) -> None:
        """Call ``on_message_gone()``, then tear the view down, after an edit
        found its message deleted.

        The gateway's message-delete event cannot do it once the edit has
        nulled ``_message``, and a bot without message intents never receives
        that event at all. Both run as their own task so they never execute
        inside the render's turn or a navigation still settling (a hook that
        sends the view again would wait on the render calling it), and the
        task is owned by the store rather than the view: ``exit()``
        cancels every task the view owns, which would cancel the teardown
        partway through its own dispatch.
        """
        current = self._message_gone_task
        if current is None or current.done():
            self._message_gone_task = self.task_manager.create_task(
                _MESSAGE_GONE_TASK_OWNER, self._teardown_after_message_gone()
            )

    async def _teardown_after_message_gone(self) -> None:
        # A send in flight may be moving the view to a new message, and a view
        # on one again has left the message found deleted.
        await self._send_finished()
        if self._message is not None:
            return
        try:
            await await_maybe(self.on_message_gone())
        except Exception as exc:
            logger.error(
                f"on_message_gone failed for {type(self).__name__}: {exc}",
                exc_info=exc,
            )
        # The gateway event may have torn the view down first, and the hook
        # may have sent the view again.
        if self._torn_down() or self._message is not None:
            return
        try:
            await await_maybe(self.on_message_delete())
        except Exception as exc:
            logger.error(
                f"on_message_delete failed for {type(self).__name__} after its message "
                f"was found deleted: {exc}",
                exc_info=exc,
            )

    def _torn_down(self) -> bool:
        """Whether teardown has run, as opposed to the view merely stopping.

        ``is_finished()`` reads discord.py's stopped future, which resolves
        *before* ``on_timeout`` is called and again on a bare ``stop()``. The
        view is intact in both cases and its message still editable, so a
        state-driven render is legitimate work. The property these seams need
        is narrower: has the teardown that clears attributes and drops the
        registry entries already run? Every teardown path unsubscribes before
        destroying the view, so the subscriber registry answers that directly
        rather than by proxy.
        """
        return self.id not in self.state_store.subscribers

    def _still_registered(self) -> bool:
        """Whether the view is still in the active-view registry or in the state."""
        store = self.state_store
        return self.id in store._active_views or self.id in store.state.get("views", {})

    def _closed(self) -> bool:
        """Whether the view has stopped, or an ``exit()`` on it has begun."""
        return self._closing or self.is_finished()

    async def _handle_state_notification(self, state, action):
        """React to state changes with update coalescing.

        When multiple dispatches trigger this callback concurrently on the
        same view (e.g. two players clicking buttons at once in a shared
        game), the second notification sets a pending flag and returns
        immediately. The first notification re-runs ``on_state_changed``
        with the latest store state after completing, capturing both
        changes in a single rebuild + edit cycle.

        Notifications that reach a torn-down view are dropped outright. The
        cross-view fan-out is fire-and-forget, so a task created ahead of a
        teardown can run after ``exit()`` completes; rebuilding then renders
        into a view that is no longer interactive, and reads attributes the
        teardown already cleared (an exited child's parent link, for
        example). A view inside its timeout window is not torn down: an
        ``on_timeout`` override that dispatches instead of delegating to
        ``super()`` keeps its subscription, and its final render ships.

        Once the ephemeral refresh button has been armed, subsequent
        notifications are dropped: the view is intentionally frozen on the
        refresh button so it stays clickable inside the 90-second
        pre-warning window. Allowing rebuilds to proceed would clobber the
        button and leave the user with no recovery path once the
        interaction token expires.
        """
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")
        try:
            await self._render_from_state()
        except RuntimeError as e:
            if not self._closed_session(e):
                raise
            self._log_render_failure(e)

    async def _render_from_state(self) -> None:
        """Run ``on_state_changed`` against current state, coalescing overlaps.

        The body of :meth:`_handle_state_notification`, and the replay a
        released reload turn schedules. Skips a torn-down or armed view.

        Never waits on the reload turn. While another task holds it (a
        reload, a load, a send, or a navigation onto this view) the render
        is recorded and replayed when the turn is released, against the
        state as it stands then. Waiting instead deadlocks whenever the
        holder is awaiting the notifying task, which is what an ``on_load``
        that dispatches from ``asyncio.gather`` or a spawned task produces.

        A notification that coalesced into a render in progress re-runs it
        once that render finishes, and the re-run passes the same checks: in
        the meantime the view can have been torn down or armed, or another
        task can have taken the reload turn.
        """
        if not self._may_render_now():
            return

        if self._update_lock.locked():
            self._update_pending = True
            return

        async with self._update_lock:
            self._render_task = asyncio.current_task()
            self._render_idle.clear()
            try:
                while True:
                    self._update_pending = False
                    await self._run_state_changed(self.state_store.state)
                    if not self._update_pending:
                        break
                    # The re-run, and a deferral of it the check below records,
                    # stand for the notifications that coalesced, not for a
                    # late render this one was.
                    late = _RENDER_ORIGIN.get()
                    if late is not None and late[0] is self:
                        _RENDER_ORIGIN.set(None)
                    if not self._may_render_now():
                        break
            finally:
                self._render_task = None
                self._render_idle.set()

    def _may_render_now(self) -> bool:
        """Whether a state-driven render may run in this task now.

        Not while a push or pop moves this view's message: the source
        records the render for a rollback, and the destination records it
        for the navigation to replay once it owns the message, since a
        render before its ``on_load`` would ship an unloaded tree. Not for a
        torn-down or armed view, or one an ``exit()`` or ``replace()`` is
        closing, whose own dispatches (a registration it retires) would
        otherwise rebuild it mid-teardown. Not while another task holds the
        reload turn, waits for it, or is sending the view either, and then the
        render is recorded for a release to replay: a send's registration
        runs without the turn, and a render there would rebuild the tree
        the send is about to validate. A waiter counts, because between a
        release and the woken waiter running the lock reads free, and a
        render started then would run beside it.
        """
        if self._away_for_navigation:
            self._missed_while_away = True
            self._defer_render(after_turn=False)
            return False
        if self._arriving_from is not None:
            self._defer_render()
            return False
        if self._torn_down() or self._refresh_armed:
            return False
        if self._closing:
            # An exit() or replace() is closing the view; one cut off before
            # the view stops replays what it declined here.
            self._defer_render()
            return False
        holder = self._reload_task or self._sending_task
        if (holder is not None and holder is not asyncio.current_task()) or self._turn_claims:
            self._defer_render()
            return False
        return True

    def _defer_render(self, *, after_turn: bool = True) -> None:
        """Record a state render for the replay, numbered as of now.

        The number orders it against content held for the replay. The
        replay's state render deferred again keeps the one it had, read from
        the late render this task is making. A source away for a push or pop
        records it for the rollback, which replays it, rather than for a
        turn's release.
        """
        late = self._late_render()
        if late is not None:
            origin = late[1]
        else:
            self._render_seq += 1
            origin = self._render_seq
        if after_turn:
            self._render_after_turn = True
        self._deferred_origin = max(self._deferred_origin, origin)

    def _edit_held(self, kwargs: Dict[str, Any], seq: int, resending: bool = False) -> bool:
        """Whether an edit of this view's message waits for a push or pop.

        A source that is away holds it for a rollback to ship, as a send
        holds one; a commit drops it, since the message is the
        destination's. A destination that has not arrived records it for the
        navigation to replay once it owns the message, with one exception:
        the navigation's own fallback edit, made through :meth:`refresh` in
        the destination's turn once the navigation's request has left. An
        edit from the destination's ``on_load`` or a rebuild hook comes
        earlier and is held; otherwise a navigation that then failed would
        leave the discarded view on screen.
        """
        if self._away_for_navigation:
            self._missed_while_away = True
            self._hold_for_replay(kwargs, seq, resending)
            return True
        source = self._arriving_from
        if source is not None and not (
            source._nav_edit_started and self._reload_task is asyncio.current_task()
        ):
            self._defer_render()
            return True
        return False

    async def on_pre_send(self, interaction: Optional[Interaction]) -> bool:
        """Pre-send veto hook -- gate the send before any work happens.

        Override to run a permission or data check (a database lookup, a
        cooldown, an entitlement gate) and decide whether the view should be
        sent at all. Return ``True`` to proceed (the default) or ``False`` to
        abort. An abort is clean: no Discord message ships and no state is
        registered, so a vetoed send leaves zero side effects.

        It runs first in the send pipeline (before :meth:`on_load`,
        placement validation, instance enforcement, and the Discord call),
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

    # Subclass config: the rebuild this view supplies for its own push and pop
    # edits when the caller passes no rebuild= (a V1 view's embed), wrapped in
    # staticmethod(). See docs/api/views.md#nav_rebuild.
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
        library runs it when a view is sent or navigated to, and two
        methods run it on request. A state change, a page turn, or a tab
        switch re-renders from what was loaded, without calling it:

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
        - :meth:`load` -- the same serialized ``on_load`` with no edit, for
          a view whose loaded data something other than its message reads.

        The synchronous ``__init__`` builds against a placeholder (an
        empty list, a "Loading..." card) so construction stays
        inspectable; ``on_load`` is where the only genuinely async work
        happens. Default is a no-op, so views without async preload pay
        nothing.
        """
        return

    async def _run_on_load(self) -> None:
        """Run ``on_load()``, warning once per class if it overruns the budget.

        ``on_load`` runs on the render path (the initial send, every push/pop
        navigation, and ``reload()``) and off it through ``load()``, whose
        callers are often a render waiting on this view's data. A preload
        slower than ``auto_defer_delay`` delays navigation and competes with
        the interaction ack, so a slow ``on_load`` is the usual silent cause
        of a sluggish panel. The warning
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

        A notification that arrives while another task is loading this view
        (a reload, a load, the send, a navigation onto it) is held and runs
        once that load finishes, so an override never sees the view
        part-way through ``on_load``.

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
        data. A reload also waits for a state render already running on the
        view in another task, so an ``on_state_changed()`` that awaits a
        separate task reloading its own view (``asyncio.gather(self.reload())``)
        never finishes; await ``self.reload()`` directly there, which runs
        inside the render. Calling ``reload()`` from inside the view's own
        ``on_load``, from a tab or wizard step builder or a cursor page fetch
        running on it, or from a ``rebuild=`` or ``nav_rebuild`` hook on a
        navigation to it, raises ``RuntimeError``: the surrounding run
        already loads the view and ships the result. So does a reload
        that would queue behind a holder waiting, directly or through other
        views, on a lock this task holds. Two views whose ``on_load`` awaits
        the other's ``reload()`` or :meth:`load` otherwise hang each other
        with nothing logged, once both start at the same time.

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
        value. A keyword a subclass acts on before ``super().reload()``
        should record a request that its ``on_load`` consumes, rather than
        edit state the fetch reads: a reload already running when this one
        queues can overwrite that state as it finishes, and the edit is
        lost without anything raising.
        """
        self._warn_if_handed_over("reload()")
        async with self._reload_turn("reload()"):
            if self._torn_down():
                # Exited or navigated away from while this call waited: a
                # render now would put live controls back on a frozen panel,
                # or over the view the message has moved on to.
                return RenderOutcome.NO_MESSAGE
            # Inside the lock and ahead of the throttle gate, so the flag
            # describes this run even when it returns at the gate.
            self._refresh_degraded = False
            # No acting waiver: the gate throttles the on_load fetch, and a
            # waiver would let a refresh button query the data source unbounded.
            if self._refresh_armed:
                # The tree is the refresh button now; on_load would replace
                # it, and the armed view takes no render that could put it back.
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
            if self._torn_down():
                return RenderOutcome.NO_MESSAGE
            return await self._reload_render()

    async def load(self) -> bool:
        """Re-run :meth:`on_load` without editing the message.

        :meth:`reload` with the render step left out, for a caller that
        needs the view's data current but has nothing to show: a view whose
        loaded state another view reads, or a cache warmed ahead of a
        render that happens elsewhere. Runs one at a time with this view's
        reloads, under the same lock, so a load never interleaves with a
        reload's ``on_load`` and a load queued behind one fetches again at
        its own turn.

        No throttle applies. The reload gate paces fetches that each end in
        an edit; a load ships nothing, so it runs as soon as its turn comes.

        Returns ``True`` when ``on_load`` ran. Returns ``False`` without
        running it while an ephemeral view is armed for its refresh
        handoff, since ``on_load`` would rebuild the tree over the refresh
        button (the view shows that button until it is reopened), and once
        the view has been torn down, whose ``on_load`` may read state the
        teardown cleared.

        Raises ``RuntimeError`` where :meth:`reload` does: when called from
        inside this view's own ``on_load``, a tab or wizard step builder or
        cursor page fetch running on it, or a navigation's rebuild hook, and
        when this view's reload lock is held by a task that is itself
        waiting, directly or through other views, on a lock this task holds.
        """
        self._warn_if_handed_over("load()")
        async with self._reload_turn("load()"):
            if self._refresh_armed or self._torn_down():
                return False
            await self._run_on_load()
            return True

    @contextlib.asynccontextmanager
    async def _reload_turn(self, method: str):
        """Hold this view's reload lock for one ``on_load`` run.

        The shared entry for :meth:`reload`, :meth:`load`, the send, and a
        navigation onto this view. Refuses the two waits it can see will
        never end: this task already holding the lock, and a holder queued,
        through any number of other views' locks, on a lock this task holds.
        A wait it cannot see through (the holder awaiting a task this one
        runs in) logs a warning once it has lasted
        ``_RELOAD_WAIT_WARN_SECONDS``. Releasing the turn replays a render
        deferred while it was held; during a send the replay defers again,
        and the send's delivery release runs it once the message exists.
        """
        current = asyncio.current_task()
        if self._reload_task is not None and self._reload_task is current:
            if self._reload_label == _NAVIGATION_TURN:
                raise RuntimeError(
                    f"{method} called while {self._reload_label} holds "
                    f"{type(self).__name__}'s reload turn in this task, from its on_load() "
                    f"or a rebuild= or nav_rebuild hook. Navigation runs on_load() before "
                    f"the hook and ships the view after it, so the view is already loaded. "
                    f"Fix: drop the {method}, and set any state the load reads before "
                    f"navigating (a constructor keyword, or push() a view built beforehand)."
                )
            raise RuntimeError(
                f"{method} called while {self._reload_label} holds "
                f"{type(self).__name__}'s reload turn in this task: from its own on_load() "
                f"or render path, or another view's reload awaited from there. That run "
                f"already loads this view. Fix: keep on_load to loading this view's own "
                f"data, and reload any other view after this one returns."
            )
        cycle = self._reload_wait_cycle(current)
        if cycle is not None:
            path = " -> ".join(type(view).__name__ for view in cycle)
            raise RuntimeError(
                f"{method} on {type(self).__name__} would wait forever: the reload "
                f"holding its lock is queued on {path}, whose lock this task holds. "
                f"Views whose on_load awaits each other's reload()/load() deadlock "
                f"when both start at once. Fix: keep on_load to loading this view's own "
                f"data, and reload any other view after this one returns."
            )
        render = self._render_task
        watchdog = None
        if (
            self._reload_lock.locked()
            or self._turn_claims
            or (render is not None and render is not current)
        ):
            # Any wait at all: behind a holder, behind a waiter a release has
            # woken but not yet run (the lock reads free then), or behind a
            # state render.
            watchdog = asyncio.get_running_loop().call_later(
                _RELOAD_WAIT_WARN_SECONDS, self._warn_long_reload_wait, method
            )
        if current is not None:
            _RELOAD_WAITING[current] = self
        self._turn_claims += 1
        acquired = False
        try:
            # A state render takes no turn and rebuilds the tree as it goes, so
            # one already running finishes first. None starts meanwhile: the
            # render gate defers while this claim is open.
            while self._render_task is not None and self._render_task is not current:
                await self._render_idle.wait()
            await self._reload_lock.acquire()
            acquired = True
        finally:
            self._turn_claims -= 1
            _RELOAD_WAITING.pop(current, None)
            if watchdog is not None:
                watchdog.cancel()
            if not acquired:
                self._replay_if_owed()
        self._reload_task = current
        self._reload_label = method
        try:
            yield
        finally:
            self._reload_task = None
            self._reload_label = None
            self._reload_lock.release()
            self._replay_if_owed()

    def _replay_if_owed(self) -> None:
        """Schedule a render deferred for the turn, once nothing holds or claims it.

        With a claim still open, that claimant's release replays instead.
        Also runs the redraw and arms the Continue button a close declined,
        once the close is undone.
        """
        if self._render_after_turn and self._reload_task is None and not self._turn_claims:
            self._render_after_turn = False
            self._deliver_later(self._replay_render())
        if self._reclaim_declined and not self._closed():
            self._reclaim_declined = False
            if self._reclaim_pending:
                self._deliver_later(self._reclaim_message(render=False))
        if self._arm_after_close and not self._closed():
            self._arm_after_close = False
            if self._refresh_handoff is not False and not self._refresh_armed:
                self._schedule_ephemeral_refresh()

    def _deliver_later(self, delivery: Awaitable[Any]) -> None:
        """Schedule ``delivery`` of what was deferred for the message: a replay or a redraw.

        A teardown that freezes the message, or tears down a view that had
        already stopped, leaves it running, so content held for the message
        still reaches it, with the controls as the teardown left them. Every
        other teardown cancels it: one that deletes the message, hands it to
        another view, discards a navigation's destination, leaves it for a
        restart, or rolls a send back.
        """
        task = self.task_manager.create_task(self.id, delivery)
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def _replay_render(self) -> None:
        """Run what this view deferred: state renders, and refreshes held during a send or a push.

        The task copies the context of whichever task scheduled it, often a
        click callback, so the bound interaction is cleared first: the replay
        is background work, owed no cooldown waiver and no share of that
        click's response slot. A raise is logged with its traceback, as the
        notification it stands in for would have been.

        An armed view ships its tree as it stands, since a rebuild would
        replace the refresh button. The render owed is then an edit the view
        could not make, such as the arming edit's retry held while a push or
        pop from it was in flight.

        Each deferred render keeps the number of when it was asked for: the
        state render that of the newest state change deferred, a held refresh
        its own. A part of the message a newer render set stays as that render
        left it, so a refresh asked for before a send that posted does not
        reach the message it posted. While a send is running, everything stays
        held for the replay after it.

        Once a close has begun, the state render is declined, as every render
        from state is then, and held content ships under the controls as the
        close left them.
        """
        _CURRENT_INTERACTION.set(None)
        if self._sending_task is not None:
            # A send began after this was scheduled: the replay after it runs
            # this, against the message it posts, or the one it stays on.
            self._render_after_turn = True
            return
        held, self._held = self._held, {}
        origin, self._deferred_origin = self._deferred_origin, 0
        if self._refresh_armed:
            try:
                await self.refresh()
            except Exception as e:
                self._log_render_failure(e)
            return
        await self._run_deferred(origin, held)

    async def _run_deferred(self, origin: int, held: Dict[str, Tuple[Dict[str, Any], int]]) -> None:
        """Run the state render deferred at ``origin`` and ship ``held``, in the order asked for.

        Content held from before the render ships first, so a render that
        navigates leaves it on the message it was asked of. Each runs even
        when another raises.
        """
        if origin:
            older = {part: entry for part, entry in held.items() if entry[1] < origin}
            if older:
                await self._ship_held(older)
                held = {part: entry for part, entry in held.items() if part not in older}
            try:
                # Stands for the state changes deferred, at the newest one's number.
                await self._render_late(self._render_from_state(), origin)
            except Exception as e:
                self._log_render_failure(e)
            if self._successor is not None:
                # That render handed the message on: what was held after it
                # is dropped, as a commit drops it.
                return
        await self._ship_held(held)

    async def _ship_held(self, held: Dict[str, Tuple[Dict[str, Any], int]]) -> None:
        """Ship refreshes held for a replay: a bare one's tree, then content by number, oldest first.

        Each edit on its own, so one that raises leaves the others to ship.
        """
        tree = held.pop("tree", None)
        if tree is not None:
            try:
                await self._render_late(self.refresh(), tree[1])
            except Exception as e:
                self._log_held_failure(e)
        for at in sorted({at for _, at in held.values()}):
            kwargs: Dict[str, Any] = {}
            for piece, piece_at in held.values():
                if piece_at == at:
                    kwargs.update(piece)
            try:
                await self._ship_stalled_render(kwargs, at)
            except Exception as e:
                self._log_held_failure(e)

    def _log_held_failure(self, error: Exception) -> None:
        """Log a held refresh that could not be sent; quietly when the view's bot has closed."""
        level = logging.DEBUG if self._closed_session(error) else logging.WARNING
        logger.log(level, f"Could not send a refresh held for {type(self).__name__}: {error}")

    def _log_render_failure(self, error: Exception) -> None:
        """Log a state render that raised; quietly when the view's bot has closed."""
        if self._closed_session(error):
            logger.debug(f"A render of {type(self).__name__} was not sent: its bot closed.")
        else:
            logger.error(f"Error notifying subscriber {self.id}: {error}", exc_info=error)

    def _stop_holding(self) -> None:
        """End the send's hold on other tasks' renders, keeping the mention rules they left.

        At once, before another task can render: those renders are newer than
        anything that reached the message, so a render after them compares
        its tree with theirs, and their held content replays under their
        rules on whichever message the view ends on.
        """
        self._sending_task = None
        # A send that posted replaces every render asked for before it began,
        # and calls made since apply after its post; one that did not leaves
        # the message as its last render did. The post's number is never below
        # the send's start, so the earlier of the two is the floor either way.
        self._take_held_rules(min(self._mention_rules[1], self._send_start))

    def _take_held_rules(self, floor: int) -> None:
        """Keep the mention rules held renders left, if their call is at least as new as ``floor``.

        A held call numbered at ``floor`` is a later refresh of the late
        render whose earlier refresh set the rules there.
        """
        rules, self._held_mention_rules = self._held_mention_rules, None
        if rules is not None and _newer_render(rules[1], floor, False):
            self._mention_rules = rules

    async def _render_late(
        self, render: Awaitable[Any], origin: int, *, resending: bool = False
    ) -> None:
        """Await ``render`` as the render numbered ``origin``, asked for earlier."""
        running = [True]
        token = _RENDER_ORIGIN.set((self, origin, resending, asyncio.current_task(), running))
        try:
            await render
        finally:
            running[0] = False
            _RENDER_ORIGIN.reset(token)

    def _late_render(self) -> Optional[tuple]:
        """The late render of this view that a refresh() made now belongs to, or ``None``.

        A refresh() belongs to it, and takes its number, when made in the
        task running it, or, while it runs, in a task whose whole body is
        one of this view's refresh() calls, which is what ``asyncio.gather``
        (and ``asyncio.wait_for`` before Python 3.12) makes of a refresh
        handed to it. A task the render started for other work is a call of
        its own when it calls refresh() itself, but a refresh it hands to
        ``asyncio.gather`` while the render still runs takes the render's
        number: asyncio does not record which task started which. Every
        refresh() once the render has returned is a call of its own, since
        every task started inside it copies the marker.
        """
        late = _RENDER_ORIGIN.get()
        if late is None or late[0] is not self:
            return None
        task = asyncio.current_task()
        if late[3] is task or (late[4][0] and self._runs_a_refresh(task)):
            return late
        return None

    def _number_for_now(self) -> int:
        """The number of an edit made now: the late render's own when made in it, else a new one."""
        late = self._late_render()
        if late is not None:
            return late[1]
        self._render_seq += 1
        return self._render_seq

    def _runs_a_refresh(self, task: Optional[asyncio.Task]) -> bool:
        """Whether ``task`` was started on one of this view's ``refresh()`` calls."""
        frame = getattr(task.get_coro(), "cr_frame", None) if task is not None else None
        return (
            frame is not None
            and frame.f_code.co_name == "refresh"
            and frame.f_locals.get("self") is self
        )

    def _hold_for_replay(
        self, kwargs: Dict[str, Any], origin: int, resending: bool = False
    ) -> None:
        """Hold a refresh deferred during a send or a push, by the part of the message it sets.

        Per part, since ``embed=`` and ``embeds=`` set one part and an edit
        refuses both, and the newer render of a part wins, by when it was
        asked for rather than when it arrived. Mention rules stay with what
        they govern: an edit rebuilds mentions from its content in V1 and
        from the tree in V2, under that edit's own ``allowed_mentions``
        (Discord's Edit Message reference). A V2 call's rules are kept for
        the view rather than with its content, and only from a call at
        least as new as the one already kept, so a late render never
        replaces a newer call's. Two refreshes of one late render share its
        number, and the later one's rules win; a render sent again, such as
        a stalled answer, keeps the rules it set when first tried.
        """
        layout = self._is_layout()
        explicit = kwargs.get("allowed_mentions")
        pieces: Dict[str, Dict[str, Any]] = {}
        for key, value in kwargs.items():
            part = _CONTENT_EDIT_FIELDS.get(key)
            if part is not None:
                pieces.setdefault(part, {})[key] = value
        if not kwargs:
            # A refresh() with no keywords ships the tree as it then stands.
            pieces["tree"] = {}
        elif explicit is not None:
            if not layout and "content" in pieces:
                pieces["content"]["allowed_mentions"] = explicit
            elif layout and not pieces:
                pieces["mentions"] = {"allowed_mentions": explicit}
        for part, piece in pieces.items():
            current = self._held.get(part)
            if current is None or origin >= current[1]:
                self._held[part] = (piece, origin)
        current = self._held_mention_rules
        if layout and (current is None or _newer_render(origin, current[1], resending)):
            digest = self._shown_digest()
            if kwargs:
                self._held_mention_rules = (explicit, origin, digest)
            elif current is not None and digest != current[2]:
                self._held_mention_rules = (None, origin, digest)

    def _declined_render(self) -> Optional["RenderOutcome"]:
        """The disposition of a render this view no longer owes, or ``None``.

        Checked by a render once it holds the reload turn, and again once it
        has built, since the view can change while the render waits. A
        torn-down view has nothing left to edit. An armed view's tree is its
        refresh button, and a rebuild would replace it with nothing able to
        bring it back. A view whose push or pop is in flight is neither: its
        render builds, and ``refresh()`` holds the edit for a rollback to
        ship, since replaying the state render would not re-run this one.
        """
        if self._torn_down():
            return RenderOutcome.NO_MESSAGE
        if self._refresh_armed:
            return RenderOutcome.SKIPPED
        return None

    def _warn_long_reload_wait(self, method: str) -> None:
        """Log a reload-turn wait that has lasted ``_RELOAD_WAIT_WARN_SECONDS``."""
        holder = self._reload_label or (
            "on_state_changed()" if self._render_task is not None else "another reload"
        )
        logger.warning(
            f"{method} on {type(self).__name__} has waited {_RELOAD_WAIT_WARN_SECONDS:g}s "
            f"for {holder} to release the view's reload turn. If that run is waiting on "
            f"this call (it awaits asyncio.gather() or a task it created, and that task "
            f"is the one waiting here), the wait never ends. Keep on_load and "
            f"on_state_changed to this view's own data, and reload or load any other "
            f"view after they return."
        )

    def _validate_loaded_tree(self) -> None:
        """Check the tree ``on_load`` built, before the send registers anything.

        The default checks nothing here; placement runs at delivery. A
        persistent view re-runs its custom_id check, since its ``send()``
        checks before ``on_load`` and would pass a tree built there.
        """

    async def _preload_send_content(self, send_kwargs: dict) -> None:
        """Add the first message's content to ``send_kwargs``, in place.

        Runs at the head of the send's delivery, after ``seed_initial_state``
        and under the turn and rollback that cover the HTTP send, so a V1
        pattern's first embed reads seeded state and a failure rolls the send
        back. The default adds nothing; a V2 view's tree is its content.
        """

    @contextlib.asynccontextmanager
    async def _within_reload_turn(self, method: str):
        """Take this view's reload turn, or run inside the one this task holds.

        For a load a notification drives: the notification can fire inline
        from inside this view's own reload (its ``on_load`` dispatched), and
        that reload is already the serialized run, so taking the turn again
        would raise rather than wait.
        """
        if self._reload_task is not None and self._reload_task is asyncio.current_task():
            yield
            return
        async with self._reload_turn(method):
            yield

    def _reload_wait_cycle(self, current) -> Optional[list]:
        """The views ``current`` would wait through to reach its own lock, or ``None``.

        Follows this view's holder to the view that holder is queued on,
        then to that view's holder, until the chain ends or comes back to
        ``current``. A view's state render counts as its holder, since a
        turn taker waits for it.
        """
        chain = []
        holder = self._reload_task or self._render_task
        seen = set()
        while holder is not None and holder not in seen:
            seen.add(holder)
            waiting_on = _RELOAD_WAITING.get(holder)
            if waiting_on is None:
                return None
            chain.append(waiting_on)
            holder = waiting_on._reload_task or waiting_on._render_task
            if holder is current:
                return chain
        return None

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
        is not the indeterminate case a cancelled bounded edit is -- the
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

    def _edit_never_landed(
        self, previous: Any, current: Any, *, cursor: str, raised: bool = False
    ) -> bool:
        """Whether a navigation cursor should rewind after an edit that did not land.

        Every paging pattern moves its cursor before the repaint -- the page,
        the wizard step, the active tab. Two render outcomes leave the screen
        where it was: a dropped edit (a transport failure, reported through
        :attr:`refresh_degraded`) and a render that raised (``raised``),
        which failed before its edit or had the edit refused. Either leaves
        the cursor pointing somewhere the screen never went, and the next
        click navigates from a position the user never saw. A cancelled
        render is neither: its edit may have landed.

        Answers the question only; the caller owns the restore, because what
        has to be rebuilt afterwards differs per pattern and per component
        version. ``previous`` of ``None`` means the caller moved no cursor
        (a data rebuild rather than a navigation), so nothing rewinds.
        """
        if previous is None or previous == current:
            return False
        if not raised and not self.refresh_degraded:
            return False
        outcome = "raised" if raised else "did not reach Discord"
        logger.debug(
            f"{cursor} change in {type(self).__name__} {outcome}; "
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
        otherwise leaves the user on the previous page with the cursor
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

    def _mark_render(self, kwargs: dict, seq: int) -> None:
        """Record that render ``seq`` reached the message, or stalled and will be sent again."""
        for field in {
            "tree",
            *(_CONTENT_EDIT_FIELDS[k] for k in kwargs if k in _CONTENT_EDIT_FIELDS),
        }:
            self._superseded_at[field] = max(self._superseded_at[field], seq)

    def _left_message(self, bound: Any) -> bool:
        """Whether a send posted meanwhile, moving the view off ``bound``."""
        message = self._message
        return message is not None and message.id != bound.id

    def _layout_mention_rules(
        self, kwargs: dict, explicit: Any, seq: int, resending: bool = False
    ) -> Optional[discord.AllowedMentions]:
        """The mention rules for a V2 edit: the newest render's that changed the message.

        A V2 edit rebuilds mentions from the whole tree under its own rules
        (Discord's Edit Message reference), so an edit carrying content
        asked for earlier must not put back rules a newer render replaced. A
        render with keywords sets them; a ``refresh()`` with none sets the
        view's own only when it changes the tree, as it would edit nothing
        otherwise. A render asked for before the one that set them ships the
        tree under them and sets nothing, and so does a render sent again
        (a stalled answer, or content held with its keywords): it set them
        when first tried. Two refreshes of one late render share its number, and
        the later one's rules win.
        """
        rules, at, tree = self._mention_rules
        if _newer_render(seq, at, resending):
            digest = self._shown_digest()
            if kwargs or explicit is not None:
                self._mention_rules = (explicit, seq, digest)
            elif digest != tree:
                self._mention_rules = (None, seq, digest)
        return self._mention_rules[0]

    async def _ship_stalled_render(self, kwargs: dict, seq: int) -> None:
        """Send a render whose one-request answer stalled, now the interaction is answered.

        A later render that reached the message has already replaced this
        one's tree. Content in its keywords (a V1 embed) is replaced only by a
        later render that set the same part of the message, and a render that
        shipped nothing (deferred, dropped) replaces nothing, so only the
        keywords still owed are sent. Every render marks the tree, so when no
        content is owed the tree is not owed either. The replay after a send
        ships the content of a refresh deferred during it the same way.
        """
        fields = {k: _CONTENT_EDIT_FIELDS[k] for k in kwargs if k in _CONTENT_EDIT_FIELDS}
        owed = {k for k, field in fields.items() if self._superseded_at[field] <= seq}
        if (fields and not owed) or (not fields and self._superseded_at["tree"] > seq):
            return
        kwargs = {k: v for k, v in kwargs.items() if k not in fields or k in owed}
        content = bool(owed)
        # The view left the message (a push or pop landed), or a close edited
        # it: the close shipped the final tree, but not content it did not
        # carry. A stop() edits nothing, so its tree is still owed.
        if self._message is None:
            return
        if not content and self._message_closed >= _MESSAGE_FREEZE:
            return
        await self._render_late(self.refresh(**kwargs), seq, resending=True)

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

        Also swallows a transport failure: a request that never reached
        Discord, which raises from aiohttp and carries no HTTP status. The
        edit is dropped, the render baseline is cleared so the next state
        change re-ships, and :attr:`refresh_degraded` reports it. Raising
        instead would put ``on_error``'s failure card in front of the user
        over a repaint that merely needs repeating. Read
        :attr:`refresh_degraded` when the caller changed something before
        the render that should not stand if the render never landed.

        This does **not** rebuild components -- call your rebuild method
        (e.g. ``build_ui()``) before calling ``refresh()``.

        On a stopped view (exited, timed out, or ``stop()``) the edit still
        ships, showing the controls as the close left them: disabled, with
        link buttons still clickable, or removed where a V1 ``exit()``
        stripped them. Inside a cooldown or rate-limit window it ships when
        the window ends; one Discord itself refuses with a 429 is dropped
        with a warning. When the close deleted the message it sends nothing.

        Calls landing inside an active cooldown window (from
        ``refresh_cooldown_ms`` or a prior 429) are deferred via a single
        scheduled task that re-enters :meth:`on_state_changed` once the
        window expires, so the deferred edit reflects the latest store
        state rather than kwargs captured at the deferred call's site.

        Args:
            **kwargs: Additional keyword arguments forwarded to
                ``message.edit()`` (e.g. ``embed=``, ``content=``).
        """
        late = self._late_render()
        resending = late is not None and late[2]
        self._warn_if_handed_over("refresh()")
        # Ahead of the no-message return, so a bad kwarg raises the same way
        # whether or not the view has been sent yet. Leaving it below would
        # reintroduce, in miniature, the send-state-dependent behavior this
        # guard exists to remove.
        self._reject_non_portable_edit_kwargs(kwargs)
        self._refresh_degraded = False
        self._render_seq += 1
        # Edits are ordered by when they were asked for, so a render made
        # later keeps the number of the one it stands for.
        seq = self._render_seq if late is None else late[1]
        if late is not None:
            # A part of the message a newer render set stays as it left it.
            for key in [k for k in kwargs if k in _CONTENT_EDIT_FIELDS]:
                if self._superseded_at[_CONTENT_EDIT_FIELDS[key]] > seq:
                    del kwargs[key]
            # A close shipped a redraw that rebuilt the tree after this render
            # was asked for (a failed navigation's nav_rebuild).
            if self._redrawn_at > seq and not any(k in _CONTENT_EDIT_FIELDS for k in kwargs):
                return RenderOutcome.SKIPPED
        # A file an earlier call sent was left at its end.
        _rewind_files(kwargs)
        # The call as made, for a hold: an edit that falls through has
        # resolved its mention rules into ``kwargs`` by then.
        asked = dict(kwargs)

        if self._edit_held(asked, seq, resending):
            return RenderOutcome.DEFERRED

        # The view's own close deleted the message.
        if self._message_closed >= _MESSAGE_DELETE:
            return RenderOutcome.NO_MESSAGE

        # During a send, another task's render replays against the new
        # message and the send's own renders ship with it. A message a
        # re-send is leaving never shows what on_load() rebuilt for the send.
        sending = self._sending_task
        if sending is not None or not self._message:
            if sending is not None and sending is not asyncio.current_task():
                self._render_after_turn = True
                self._hold_for_replay(kwargs, seq, resending)
                return RenderOutcome.DEFERRED
            return RenderOutcome.NO_MESSAGE

        if self.is_finished():
            # A render after the close (a modal submitted late, a caller's own
            # update loop) shows the controls as the close left them: removed
            # where a V1 exit stripped them, otherwise disabled.
            if self._controls_removed:
                self.clear_items()
            else:
                self._disable_items(self)
            if self._torn_down():
                self._closed_render_kwargs = dict(kwargs)

        # Whether this edit answers a click (or a modal opened from one) on
        # this view's own message. Resolved before the gate, which it
        # decides. Broader than the one-request test below: an acked slot
        # still has a user waiting and only changes the endpoint.
        interaction = _CURRENT_INTERACTION.get()
        acting = (
            interaction is not None
            and interaction.type in _ACTING_INTERACTION_TYPES
            and interaction.message is not None
            and interaction.message.id == self._message.id
        )

        # An edit answering a click waives the cooldown but never a 429 window.
        answering = acting or resending
        now = time.monotonic()
        wait = self._throttle_until(acting=answering) - now
        if wait > 0:
            self._queue_deferred_refresh(wait)
            return RenderOutcome.DEFERRED

        store = self.state_store
        perf_on = getattr(store, "_perf_enabled", False)
        t0 = time.perf_counter() if perf_on else 0.0
        skipped = False
        rendering = None
        # The message this render is for. One the view leaves while the edit
        # is in flight does not get the render's fallback edit.
        bound = self._message

        try:
            # Rebuilds outside build_ui (page turns, tab switches, step
            # changes) create items with random ids; a click sent from the
            # previous render would then match no item. Idempotent.
            self._stabilize_custom_ids()

            # Theme-managed accents resolve against the live view theme
            # before the digest, so a runtime theme change alters the
            # digest and ships as a re-render instead of being skipped.
            self._apply_theme_defaults()
            self._sync_back_buttons()

            # Skipped when the tree matches the last render. Not with kwargs:
            # embed or content live outside the tree the digest covers.
            if not kwargs and self._last_tree_digest is not None:
                current_digest = self._compute_tree_digest()
                if current_digest == self._last_tree_digest:
                    skipped = True
                    return RenderOutcome.SKIPPED

            # A tree Discord would refuse raises here, naming the node,
            # instead of coming back as an HTTP 400.
            self._check_placement()

            # Explicit on every path: ``Message.edit`` forwards the client's
            # default only with ``content``, which a V2 view never passes.
            # After the digest skip, which requires empty kwargs.
            rules = kwargs.pop("allowed_mentions", None)
            if self._is_layout():
                rules = self._layout_mention_rules(kwargs, rules, seq, resending)
            mentions = self._resolve_allowed_mentions(rules)
            if mentions is not None:
                kwargs["allowed_mentions"] = mentions

            # In flight from here to the last request, so a close waiting on it
            # also waits for an attempt that falls through. Taken before the
            # first request: before Python 3.12 the bound starts it a tick late.
            rendering = asyncio.get_running_loop().create_future()
            self._edits_pending.add(rendering)
            # One request both answers the click and edits the message
            # (docs/guide/performance.md). It is scoped to this click's own
            # token, so an ephemeral view qualifies too. The bound sits below
            # auto_defer_delay because the ack rides it, and discord.py marks
            # the response done only after the await, so a cancelled attempt
            # leaves the interaction unanswered for the timer to ack.
            slot_open = acting and not interaction.response.is_done()
            if slot_open:
                fast_path_timeout = max(0.5, self.auto_defer_delay - 1.0)
                try:
                    shipped_digest = await _bounded_wait(
                        self._edit_and_digest(
                            lambda: interaction.response.edit_message(view=self, **kwargs)
                        ),
                        timeout=fast_path_timeout,
                    )
                    self._has_rendered = True
                    self._last_tree_digest = shipped_digest
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown(acting=answering)
                    self._mark_render(kwargs, seq)
                    self._release_left_message(interaction.message.id)
                    self._redrive_dynamic_items()
                    return RenderOutcome.RENDERED
                except asyncio.TimeoutError as e:
                    if isinstance(e, aiohttp.ClientError):
                        self._note_transport_failure(e, where="Refresh")
                        return RenderOutcome.DROPPED
                    logger.debug(
                        f"Acting-view fast path exceeded {fast_path_timeout:.2f}s "
                        f"in {type(self).__name__}; fall-through skipped, "
                        f"auto-defer timer handles ack"
                    )
                    # No second attempt: on top of the cancelled one it spends
                    # the timer's ack budget (an ephemeral's webhook edit can
                    # take up to edit_timeout), and under Discord-side latency
                    # the click can fail.
                    # Whether Discord applied the cancelled edit is unknown,
                    # so the baseline is cleared, and the code that answered
                    # the interaction sends the render again once acked.
                    self._last_tree_digest = None
                    self._mark_render(kwargs, seq)
                    _note_stalled_render(self, kwargs, seq)
                    return RenderOutcome.DROPPED
                except discord.InteractionResponded:
                    # The timer acked between is_done() and edit_message's own
                    # guard, which raises before any request (and is not an
                    # HTTPException): nothing shipped, so the next path does.
                    pass
                except DISCORD_CALL_ERRORS as e:
                    if self._handle_rate_limit(e):
                        return RenderOutcome.DEFERRED
                    # Any other HTTP error falls through to the channel path
                    # so a transient failure on the interaction endpoint does
                    # not lose the edit entirely. The failed request streamed
                    # any file it carried.
                    _rewind_files(kwargs)

            # A fast path that fell through has awaited, and a push or pop from
            # this view can have begun meanwhile. Its edit must be the last.
            if self._edit_held(asked, seq, resending):
                return RenderOutcome.DEFERRED
            if self._left_message(bound):
                return RenderOutcome.DROPPED

            # The channel endpoint ignores embed edits on an interaction's
            # message; the interaction webhook applies them until its token
            # expires.
            if ("embed" in kwargs or "embeds" in kwargs) and self._webhook_message:
                target = self._webhook_message
                try:
                    shipped_digest = await self._bounded(
                        self._edit_and_digest(functools.partial(target.edit, view=self, **kwargs))
                    )
                    self._has_rendered = True
                    self._last_tree_digest = shipped_digest
                    if perf_on:
                        store._record_edit()
                    self._stamp_cooldown(acting=answering)
                    self._mark_render(kwargs, seq)
                    self._release_left_message(target.id)
                    self._redrive_dynamic_items()
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
                        # this handle is the only way to edit this message's
                        # embeds, so it is kept.
                        self._note_transport_failure(e, where="Webhook edit")
                        return RenderOutcome.DROPPED
                    # Token expired (15-min lifetime) -- fall through to channel endpoint
                    if self._webhook_message is target:
                        self._webhook_message = None
                    _rewind_files(kwargs)

            # A fast path or webhook edit that falls through has awaited: a push
            # or pop can have begun, or a concurrent refresh found the message
            # deleted.
            if self._edit_held(asked, seq, resending):
                return RenderOutcome.DEFERRED
            if self._left_message(bound):
                return RenderOutcome.DROPPED
            message = self._message
            if message is None:
                return RenderOutcome.NO_MESSAGE
            try:
                shipped_digest = await self._bounded(
                    self._edit_and_digest(functools.partial(message.edit, view=self, **kwargs))
                )
                self._has_rendered = True
                self._last_tree_digest = shipped_digest
                if perf_on:
                    store._record_edit()
                self._stamp_cooldown(acting=answering)
                self._mark_render(kwargs, seq)
                self._release_left_message(message.id)
                self._redrive_dynamic_items()
                return RenderOutcome.RENDERED
            except discord.NotFound:
                if self._message is not None and self._message is not message:
                    # Sent anew while the edit was in flight: the deleted
                    # message is one the view has left, and the send shipped
                    # its tree to the new one.
                    return RenderOutcome.DROPPED
                if self._message_closed >= _MESSAGE_DELETE:
                    # The view's own close deleted it while this edit was in
                    # flight: not a deletion the hook reports.
                    return RenderOutcome.NO_MESSAGE
                # Deleted out from under the view. Null the ref so later
                # refreshes stop at the top-of-method guard; the hook and the
                # teardown run on their own task, outside this render's turn.
                self._message = None
                self._schedule_message_gone_teardown()
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
                    # An ephemeral past its 15-minute token: an expected end,
                    # as exit() and on_timeout() treat it. Raising would log
                    # an ERROR through the subscriber wrapper on every dispatch.
                    logger.debug(
                        f"Refresh skipped: ephemeral webhook token expired "
                        f"for {type(self).__name__}."
                    )
                    return RenderOutcome.NO_MESSAGE
                elif isinstance(e, aiohttp.ClientError):
                    # The request never reached Discord and the state is
                    # committed, so the next refresh re-ships it. Raising would
                    # put on_error's failure card over a repaint.
                    self._note_transport_failure(e, where="Refresh")
                    return RenderOutcome.DROPPED
                elif not self._handle_rate_limit(e):
                    raise
                return RenderOutcome.DEFERRED
        finally:
            self._release_render(rendering)
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

        Three exception types reach here, since the shared catch-tuple covers
        transport failures, and only one of them carries the delay as an
        attribute. An ``aiohttp.ClientError`` is never a rate limit, so it
        falls straight through to ``False`` and its caller degrades.
        ``discord.RateLimited`` exposes ``retry_after`` directly. It appears
        only when the client sets ``max_ratelimit_timeout``, and from either
        side of the request: the bucket can predict the wait is too long and
        refuse to send, or a real 429 can come back asking for longer than
        the ceiling allows. Either way the delay it carries is authoritative. It subclasses
        ``DiscordException`` rather than ``HTTPException``, so every seam
        that calls this names it explicitly in its ``except``.

        A 429 ``HTTPException`` carries no ``retry_after`` at all
        (discord.py's parser keeps only ``code`` and ``message`` from the
        body), so the delay is read from the ``Retry-After`` response
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
        notification that could repair it, so a 429 on the arming edit
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
        if not self._message:
            return
        if self._torn_down():
            # A closed view's render Discord refused is not replayed: discord.py
            # closes the files a sent edit carried, and no teardown would ever
            # end a chain of retries. One the window held before sending is
            # replayed by _deferred_refresh.
            if self._closed_render_kwargs is not None:
                self._closed_render_kwargs = None
                logger.warning(
                    f"Discord rate-limited the last render of {type(self).__name__}, "
                    f"which has closed; the render was dropped."
                )
            return
        wait = self._ratelimit_not_before - time.monotonic()
        if wait > 0:
            self._queue_deferred_refresh(wait)

    def _queue_deferred_refresh(self, wait: float) -> None:
        """Own the single deferred-refresh slot.

        One task, whoever is asking (the cooldown gate, the reload gate, or
        a rate-limit that just discarded an edit), so a burst queues one
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

        Background edits answer to both windows. An *acting* edit (one made
        in direct response to a user's click on this view's own message)
        waives the library's cooldown but never Discord's rate limit: a user
        who pressed a button is owed a response, while an endpoint returning
        429 is owed silence regardless of who asked.
        """
        if acting:
            return self._ratelimit_not_before
        return max(self._cooldown_not_before, self._ratelimit_not_before)

    async def _bounded(self, coro):
        """Await a Discord HTTP coroutine under the ``edit_timeout`` ceiling.

        The bound cancels the request when it stalls past
        ``edit_timeout`` seconds; aiohttp closes the connection on
        cancellation, so a hung socket cannot pin the awaiting code.
        ``edit_timeout = None`` awaits the coroutine directly with no
        ceiling. Raises ``asyncio.TimeoutError`` on stall so the caller
        can release the interaction lock and recover on the next edit.
        """
        if self.edit_timeout is None:
            return await coro
        return await _bounded_wait(coro, self.edit_timeout)

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
        return await _bounded_wait(coro, timeout=max(0.5, self.auto_defer_delay - 1.0))

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

    async def _reclaim_content(self) -> Tuple[Dict[str, Any], int]:
        """The content an owed redraw would have shipped, for a teardown edit, and its number.

        A failed navigation owes the view a redraw with the content its
        ``nav_rebuild`` names. A freeze that begins before the redraw runs
        that hook, or runs in its place for a view its own timeout stopped,
        carries the content instead, or a V1 message keeps the discarded
        view's embed. Gathered before the freeze, so a hook that rebuilds the
        tree is frozen with it, and numbered as a render asked for now (or
        as the late render it runs in), so content held from before it does
        not ship over it afterwards. The number comes back only when the
        hook changed what the tree shows; the edit that carries the redraw
        records it in ``_redrawn_at`` once it lands.
        """
        if not self._reclaim_pending:
            return {}, 0
        self._reclaim_pending = False
        nav_rebuild = getattr(self, "nav_rebuild", None)
        if nav_rebuild is None:
            return {}, 0
        shown, redrawn = self._shown_digest(), 0
        try:
            result = await await_maybe(nav_rebuild(self))
            at = self._number_for_now()
            if self._shown_digest() != shown:
                redrawn = at
            if not isinstance(result, dict):
                return {}, redrawn
            self._reject_non_portable_edit_kwargs(result)
            self._mark_render(result, at)
            # A file the hook returned may have gone out with an earlier edit.
            content = dict(result)
            _rewind_files(content)
            return content, redrawn
        except Exception as e:
            logger.error(
                f"nav_rebuild for {type(self).__name__}'s teardown edit raised: {e}",
                exc_info=True,
            )
            return {}, redrawn

    async def _ship_freeze(self, target, content: Dict[str, Any], redrawn: int) -> None:
        """Edit this view's message to the frozen ``target``, with an owed redraw's ``content``.

        The redraw's own ``allowed_mentions`` win over the view's. A V2
        freeze's rules are recorded at the freeze's number, unless a newer
        render's are, so content a spared delivery ships after it goes out
        under them. Made inside a late render, the freeze takes that
        render's number, so the render's own later edits are not older than
        it. ``redrawn`` is the redraw's number, recorded once the edit lands.
        """
        kwargs = {**self._freeze_edit_kwargs(target), **content}
        if self._is_layout():
            at = self._number_for_now()
            if _newer_render(at, self._mention_rules[1], False):
                self._mention_rules = (content.get("allowed_mentions"), at, self._shown_digest())
        await self._bounded(self._message.edit(**kwargs))
        self._redrawn_at = max(self._redrawn_at, redrawn)

    def _freeze_edit_kwargs(self, view=None) -> Dict[str, Any]:
        """Edit kwargs for a teardown edit that ships the frozen tree.

        The teardown edits (``on_timeout``, ``exit()``'s V2 branch, which
        the ephemeral reopen cleanup also reaches) hand Discord the same
        mention-bearing tree ``refresh()`` does, so they owe the same
        mention rules. Without them the last edit a view ever makes is the
        one edit that ignores the view's own ``allowed_mentions``. ``view``
        defaults to this view; a restored view that never rendered passes
        the frozen copy of its message instead, which still owes this
        view's rules.
        """
        kwargs: Dict[str, Any] = {"view": self if view is None else view}
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
        edits), ``_apply_navigation_edit`` (push/pop edits), and
        ``_teardown_edit_target`` (the freeze edit, which logs and skips on a
        refusal instead of raising). The
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

        A torn-down view cannot re-render from state, so the last render asked
        of it (an ``on_timeout()`` override's final card) ships as the tree
        stands, with the keywords it was called with.

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
        # The task copied the scheduling click's context, and that click was
        # answered long ago: left bound, the render would claim the acting
        # waiver and edit through a spent response slot. The copy is this
        # task's own, so clearing it does not reach the caller.
        _CURRENT_INTERACTION.set(None)
        try:
            while True:
                await asyncio.sleep(wait)
                if not self._message or (self._torn_down() and self._closed_render_kwargs is None):
                    return
                wait = self._throttle_until() - time.monotonic()
                if wait <= 0:
                    break
            if self._torn_down():
                kwargs, self._closed_render_kwargs = self._closed_render_kwargs, None
                await self.refresh(**kwargs)
                return
            if self._refresh_armed:
                # Rebuilding here would clear the refresh button, and the
                # armed flag then drops every notification that could put it
                # back -- the user would be left with a stale panel and no
                # recovery path once the webhook token expires.
                await self.refresh()
                if self.refresh_degraded:
                    # A transport drop retries while the token can carry an
                    # edit (a 429 re-queues through _handle_rate_limit). Past
                    # the cliff the edit answers 401, which this flag does not
                    # report, so the chain ends there.
                    self._queue_deferred_refresh(_ARMING_RETRY_SECONDS)
            elif self._reload_pending:
                self._reload_pending = False
                kwargs = self._pending_reload_kwargs
                self._pending_reload_kwargs = {}
                await self.reload(**kwargs)
            else:
                # Through the notification entry, so a render landing while
                # another task loads the view waits for it, and one landing
                # during a notification's render coalesces into it.
                await self._render_from_state()
            if (
                self._reload_pending
                and not self._refresh_armed
                and not self._torn_down()
                and self._message
            ):
                # A reload coalesced during the render above found this task
                # alive and queued nothing, so it would stay latched with no
                # task to run it. Not while armed: the armed branch never
                # clears the flag, and successors would respawn until the cliff.
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
        # Named for the undo step, which follows the panel from a view already
        # torn down by a push or pop: dispatch() right after push() returns.
        token = _SOURCE_VIEW.set(self)
        try:
            return await self.state_store.dispatch(action_type, payload, source_id=self.id)
        finally:
            _SOURCE_VIEW.reset(token)

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

    @property
    def _registry_message_id(self) -> Optional[str]:
        """The message id of the persistent registration this view owns or carries.

        A persistent panel owns the registration it sent or was restored under;
        a view it navigated to carries the same id without the key. ``exit()``
        sends it with the unregister, so a superseded panel cannot remove the
        row its successor owns, and ``_message`` is already ``None`` by the time
        a deleted message's cleanup exits the view. Written through this setter
        so the store's lookup index follows every write.
        """
        return self.__dict__.get("_registry_message")

    @_registry_message_id.setter
    def _registry_message_id(self, value: Optional[str]) -> None:
        previous = self.__dict__.get("_registry_message")
        self.__dict__["_registry_message"] = value
        store = getattr(self, "state_store", None)
        if store is not None and previous != value:
            store._reindex_registry_message(self, previous, value)

    # The registration a panel's swap superseded (``retire_previous_on_send =
    # False``), carried to the views it navigated to, so closing one of them
    # hands it back as the panel's own ``exit()`` would.
    _carried_hand_back: Optional[dict] = None

    def _hand_back_record(self, persistence_key: str) -> Optional[dict]:
        """The superseded registration for ``persistence_key`` this view holds, or ``None``."""
        for record in (getattr(self, "_superseded_registration", None), self._carried_hand_back):
            if record is not None and record.get("persistence_key") == persistence_key:
                return record
        return None

    async def _reregister_live_predecessor(self, key: str) -> None:
        """Register the most recent other live panel under ``key``, if any.

        Called only for a panel with ``retire_previous_on_send = False``, the
        setting under which an earlier panel stays live beside it, or for a
        view such a panel pushed to. A finished panel is skipped: a caller
        that stopped it has already retired it. So is one a push is still
        bringing in, which may yet be discarded.

        A panel that has pushed no longer holds the key: the view on its
        message carries its registration instead. When no panel holding the
        key is live, the registration this panel superseded goes back to the
        view on that message, recorded with the panel's own class and
        arguments, so a restart reattaches the panel there.
        """
        for view in self.state_store._views_for_key(key):
            if (
                view is not self
                and view._message is not None
                and not view.is_finished()
                and view._arriving_from is None
            ):
                await view._register_persistent(view._message)
                return
        superseded = self._hand_back_record(key)
        if superseded is None:
            return
        message_id = superseded.get("message_id")
        for view in self.state_store._views_for_message(message_id):
            if not view.is_finished() and view._registration_source(key) is not None:
                payload = ActionCreators.persistent_view_registered(
                    persistence_key=key,
                    class_name=superseded.get("class_name"),
                    message_id=message_id,
                    channel_id=superseded.get("channel_id"),
                    guild_id=superseded.get("guild_id"),
                    user_id=superseded.get("user_id"),
                )
                await self.state_store.dispatch("PERSISTENT_VIEW_REGISTERED", payload)
                return

    async def _hand_back(self, key: str) -> None:
        """Hand ``key`` back to its live predecessor, reporting rather than raising."""
        try:
            await self._reregister_live_predecessor(key)
        except Exception as e:
            # Reported rather than raised, like the registration in send(): the
            # teardown still has to run, and a caller rolling a swap back would
            # otherwise be left with both panels live.
            logger.error(
                f"Handing {key!r} back to the live predecessor raised "
                f"{type(e).__name__}: {e}. The registration is removed and no "
                f"panel holds it, so none is restored after a restart; re-send "
                f"the panel to register it again.",
                exc_info=e,
            )

    def _registration_source(self, persistence_key: str) -> Optional[tuple]:
        """The constructor kwargs and ``kwargs_schema_version`` a registry row
        under ``persistence_key`` records for this view, or ``None``.

        The view's own when it holds the key. A view a panel pushed to carries
        the panel's registration without the key, so the panel's arguments
        are the navigation entry ``pop()`` rebuilds it from: its own would
        restore the wrong view in the panel's place.
        """
        if self._persistence_key == persistence_key:
            return self._init_kwargs, int(getattr(type(self), "kwargs_schema_version", 1))
        for entry in reversed(self._nav_stack):
            kwargs = entry.get("kwargs") or {}
            if kwargs.get("persistence_key") == persistence_key:
                cls = _view_class_registry.get(entry.get("class_name"))
                return kwargs, int(getattr(cls, "kwargs_schema_version", 1))
        return None

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
        # The scope the limit counted under, so the rejection is worded for it.
        scope = self._instance_root_scope or self.instance_scope
        existing = self.state_store._get_active_views(view_type, scope_key)
        # An ephemeral reopen sends its replacement while the old view is still
        # registered; counting it would exit the old view mid-send, before the
        # carries (children, undo, session) read it.
        replacing_id = getattr(self, "_replacing_view_id", None)
        if replacing_id is not None:
            existing = [v for v in existing if v.id != replacing_id]
        # A replace() destination is registered before its caller sends it;
        # counting it would make the send exit the view it is sending.
        existing = [v for v in existing if v is not self]
        overflow = len(existing) - self.instance_limit + 1

        if overflow <= 0:
            return

        if self.instance_policy == "reject":
            raise InstanceLimitError(display_type, self.instance_limit, scope=scope)

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
            raise InstanceLimitError(display_type, self.instance_limit, scope=scope)

        # Pre-scan: check all candidates before exiting any, so the
        # replace path never destroys views and then raises on a later
        # protected one.
        if not self._persistent:
            for old_view in to_replace:
                if getattr(old_view, "_persistent", False):
                    raise InstanceLimitError(display_type, self.instance_limit, scope=scope)

        # Notify each view before tearing it down. on_replaced fires
        # while the view is fully intact (message, participants, channel).
        # Errors are logged but never block the new view's send().
        for old_view in to_replace:
            await old_view._call_hook_safe(old_view.on_replaced)

        # Oldest first; each old view's replace_policy deletes its message or
        # closes it as exit() does. One whose push or pop lands meanwhile has
        # handed its place on, and the view that took it is exited instead.
        for old_view in to_replace:
            await old_view._exit_or_successor(lambda view: view.replace_policy == "delete")

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
            session_origin: Key of a navigation chain's root view
                (``module.QualName``, or its ``session_class_key``). A view
                reached by ``push()`` counts under its root, with the root's
                ``instance_scope``; pass the root's key to check a pushed
                class the same way. Defaults to this class's own key. The
                limit read is the root class's: one set on a panel with
                ``set_class_attribute()`` is not visible here.
            state_store: Optional ``StateStore`` instance. Uses the
                singleton if not provided.

        Returns:
            ``True`` if an instance slot is available (or no limit is set).
        """
        # A chain is indexed under its root, with the root's scope and limit,
        # so a check for the chain reads them from the root.
        governing = cls
        if session_origin is not None:
            governing = _class_for_session_key(session_origin) or cls
        if governing.instance_limit is None:
            return True

        from ..state.singleton import get_store

        store = state_store or get_store()

        scope = governing.instance_scope
        # Falsy ids are treated as absent here, matching how the instance
        # index itself keys views.
        scope_key = store.scope_key(scope, user_id=user_id or None, guild_id=guild_id or None)
        if scope_key is None:
            return True

        view_type = session_origin or cls._class_session_key()
        existing = store._get_active_views(view_type, scope_key)
        return len(existing) < governing.instance_limit

    # // ========================================( Participants )======================================== // #

    def _root_instance_limit(self):
        """The ``instance_limit`` of this view's chain root, override included.

        A view is its own root until a push files it under another view; a
        chain whose root limit was not carried (restored or older) reads the
        root class's value.
        """
        if self._instance_root_class is None:
            return self.instance_limit
        if self._instance_root_limit is not _ROOT_LIMIT_UNKNOWN:
            return self._instance_root_limit
        root = _class_for_session_key(self._instance_root_class)
        return root.instance_limit if root is not None else self.instance_limit

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
                interaction over ``self.interaction`` so the joiner, not
                the view owner, sees the rejection ephemeral.

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

        # 1. Per-user session overflow check. A view reached by push() is
        # indexed under its chain's root, so the root's limit is the one that
        # holds there; the pushed class's own would let a participant join
        # past it.
        limit = self._root_instance_limit()
        if limit is not None:
            scope_key = self.state_store._build_instance_scope_key(self, user_id=user_id)
            owner_key = self.state_store._build_instance_scope_key(self)
            if scope_key is not None and scope_key != owner_key:
                view_type = self._instance_root_class or type(self)._class_session_key()
                existing = self.state_store._get_active_views(view_type, scope_key)
                if len(existing) >= limit:
                    error = InstanceLimitError(type(self).__name__, limit, blocked_user_id=user_id)
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

    def stop(self) -> None:
        """Stop the view and repair discord.py's dynamic-item registry.

        ``super().stop()`` reaches discord.py's ``ViewStore.remove_view``
        via the cancel callback set in ``_start_listening_from_store``,
        which pops every dynamic-item pattern THIS view contributed from a
        dict keyed by compiled template, shared by every message carrying
        a matching ``DynamicPersistentButton`` subclass, with no refcount
        against other live views using the same pattern. Wrapping the one
        choke point every teardown path reaches (``exit()``, ``replace()``,
        ``_commit_navigation()``, or a caller's own direct ``stop()``
        call) means no future teardown path can reintroduce the gap by
        forgetting the repair. discord.py's own timeout dispatch bypasses
        this method entirely (it invokes the cancel callback directly, not
        ``stop()``), so :meth:`_dispatch_timeout` carries its own explicit
        repair.
        """
        super().stop()
        self._redrive_dynamic_items()

    def _reregister_dispatch(self) -> None:
        """Re-add this view's items to the discord.py view store routing its clicks.

        The store keys a message's items by custom_id, and a view stopping on
        the same message removes every key it registered, the ones this view
        reuses included, which leaves those buttons unanswered. The store is
        the one discord.py registered this view through, read off the
        callback it installed; a view it never registered has none, and
        nothing is done.
        """
        store = self._view_store()
        if store is None or self.is_finished():
            return
        store.add_view(self, self._cache_key)

    def _view_store(self):
        """The discord.py view store routing this view's clicks, or ``None``.

        Read off the cancel callback discord.py installs when it registers the
        view; a view it never registered has none.
        """
        callback = getattr(self, "_BaseView__cancel_callback", None)
        return getattr(getattr(callback, "func", None), "__self__", None)

    def _client(self):
        """The client this view was sent through, or ``None``.

        The one routing its clicks when discord.py stores the view; a view it
        never stores (nothing clickable, sent as a followup or to a channel)
        answers by the interaction, context, or restore that sent it, or by
        the client its message was fetched through, and a view a push is still
        bringing in answers for the view it replaces.
        """
        store = self._view_store()
        if store is not None:
            return store._state._get_client()
        context = self.context
        for client in (
            getattr(self.interaction, "client", None),
            getattr(context, "bot", None),
            getattr(getattr(context, "_state", None), "_get_client", lambda: None)(),
            getattr(self, "_bot", None),
            getattr(getattr(self._message, "_state", None), "_get_client", lambda: None)(),
        ):
            if isinstance(client, discord.Client):
                return client
        source = getattr(self, "_arriving_from", None)
        return source._client() if source is not None else None

    def _client_closed(self) -> bool:
        """Whether the client this view was sent through has closed.

        A request through a closed discord.py client raises ``RuntimeError``
        ("Session is closed"), which is part of the shutdown, not a failure.
        """
        client = self._client()
        return client is not None and client.is_closed() is True

    def _closed_session(self, error: BaseException) -> bool:
        """Whether ``error`` is a request refused because this view's bot has closed."""
        return isinstance(error, RuntimeError) and self._client_closed()

    def _release_for_restart(self) -> None:
        """Take this view out of the process, as a process restart would.

        The bot it was sent through has closed. Its timer and tasks stop,
        code awaiting ``wait()`` ends as it does when the process ends, it
        leaves the subscribers, freeing its instance slot, and nothing is
        edited, since its client cannot make the edit. The registration id is
        dropped, so an ``exit()`` on it later leaves alone the panel restored
        on that message; the persistent row itself is kept for the restore.
        """
        # A task of this view that closed the bot goes on after the close.
        self.task_manager._cancel_for_teardown(self.id, keep_caller=True)
        stopped = getattr(self, "_BaseView__stopped", None)
        if stopped is not None and not stopped.done():
            stopped.cancel()
        self.stop()
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        self._message = None
        self._registry_message_id = None

    def _release_message(self, message_id: int, store) -> None:
        """Stop discord.py routing a message this view left to this view.

        discord.py keys a view's buttons by message, and forgets only the
        newest one when the view stops, so a message the view was sent away
        from would keep routing its clicks here. It also feeds each edit of a
        message it tracks back into the view's items: freezing the old
        message would disable the live panel's buttons. Only this view's
        entries go; another view restored onto the same message keeps its.
        """
        if store is None:
            return
        if store._synced_message_views.get(message_id) is self:
            store.remove_message_tracking(message_id)
        entries = store._views.get(message_id)
        if entries:
            for key, item in list(entries.items()):
                if getattr(item, "view", None) is self:
                    del entries[key]
            if not entries:
                store._views.pop(message_id, None)

    def _let_go_of_message(self, message, store) -> None:
        """Stop a message this view was sent away from routing to it or taking its edits."""
        self._release_left_message(message.id, store)
        if getattr(self._webhook_message, "id", None) == message.id:
            # An embed edit through a handle on the old message would repaint
            # the message this closes.
            self._webhook_message = None

    def _release_left_message(self, message_id: int, store=None) -> None:
        """Stop a message this view has left routing to it, keyed where the view now is.

        discord.py stores the view for the message on every edit that lands,
        so an edit still in flight when the view was sent again would route
        the old message's clicks to the panel now on the new one, and key the
        view to the old message. ``store`` defaults to the view's own.
        """
        current = self._message
        store = store if store is not None else self._view_store()
        if store is None or (current is not None and current.id == message_id):
            return
        if (
            current is not None
            and getattr(self, "_cache_key", None) == message_id
            and not self.is_finished()
        ):
            store.add_view(self, current.id)
        self._release_message(message_id, store)

    async def _leave_message(self, message, ephemeral: bool, store, carried, shown) -> None:
        """Close the message a view sent again has moved off, as its ``exit()`` would.

        The message stops routing to this view, and a registration the view
        still carries for it retires, since the panel it named closes there
        (one the send moved to the new message stays). Then
        ``exit_policy`` decides: ``"delete"`` removes the message, and
        ``"disable"`` freezes it as it was shown when the send began
        (``shown``), every control disabled; a V1 message loses its buttons
        and keeps its embed. ``store`` is the view store as it was then, since
        a ``stop()`` during the send detaches it. A failure is logged: the
        view is live on its new message either way.
        """
        self._let_go_of_message(message, store)
        try:
            # A bot's close clears the view's registration id, which keeps the
            # rows a restart restores from.
            if self._registry_message_id is not None:
                await self._retire_carried_registrations(carried)
            # Still naming the message it left: the send was stopped, closed,
            # or cut before the registration moved, and that message closes
            # below.
            if carried is not None and self._registry_message_id == carried:
                await self._retire_registration_left_behind(self._message)
            await self._close_left_message(message, ephemeral, shown)
        except asyncio.CancelledError:
            # A cancel the send holds cuts the step it lands in. The message
            # already stopped routing to the view, so left as it is it would
            # keep controls that answer nothing: its close finishes on a task
            # of its own, which needs nothing the send holds.
            self.task_manager.create_task(
                _CLOSE_RESUME_TASK_OWNER, self._close_left_message(message, ephemeral, shown)
            )
            raise

    async def _close_left_message(self, message, ephemeral: bool, shown) -> None:
        """Close the message a re-send left by ``exit_policy``, logging a failure."""
        name = type(self).__name__
        try:
            # An edit of the old message still in flight would land after the
            # close and show live controls again; the render releases the
            # routing it re-creates itself (_release_left_message).
            await self._wait_for_own_edits("Re-sending")
            await self._close_other_message(message, shown)
        except discord.NotFound:
            pass
        except (*DISCORD_CALL_ERRORS, asyncio.TimeoutError) as e:
            if ephemeral and getattr(e, "status", None) == 401:
                # The old ephemeral's webhook token has expired.
                logger.debug(f"{name} could not close the ephemeral message it was sent from.")
                return
            cause = (
                f"stalled past {self.edit_timeout}s"
                if isinstance(e, asyncio.TimeoutError) and not isinstance(e, aiohttp.ClientError)
                else describe_discord_error(e)
            )
            logger.warning(
                f"{name} was sent again, but closing the message it left did not reach "
                f"Discord ({cause}); that message keeps controls that no longer answer."
            )
        except RuntimeError:
            if not self._client_closed():
                raise
            logger.debug(f"{name} left its earlier message as it was: its bot closed.")

    async def _close_other_message(self, message, components) -> None:
        """Delete a message this view no longer owns, or freeze it, as ``exit_policy`` says.

        The message a re-send left, or a persistent panel's from an earlier
        run. A frozen V2 message shows ``components``, what it already
        showed, rebuilt into a stopped copy so the edit registers nothing.
        """
        if self.exit_policy == "delete":
            await self._bounded(message.delete())
        elif not self._is_layout():
            await self._bounded(message.edit(view=None))
        else:
            copy = self._frozen_copy_of(components)
            if copy is not None:
                await self._bounded(message.edit(**self._freeze_edit_kwargs(copy)))

    def _redrive_dynamic_items(self) -> None:
        """Repair discord.py's dynamic-item registry.

        Re-drives the full registry with the bot (the same recovery
        ``PersistenceManager.reattach()`` already performs on every pass),
        harmless and idempotent for a view that carries no dynamic item.
        Two distinct discord.py mechanisms wipe the registry, and this
        method is called from a seam covering each: :meth:`stop` and
        :meth:`_dispatch_timeout` repair the teardown-time wipe
        (``ViewStore.remove_view``, keyed on the stopping view's own
        pattern set). ``refresh()``'s three successful-edit returns and
        navigation's destination edit repair a SEPARATE, steady-state wipe:
        every live-view edit re-runs ``ViewStore.add_view``, which diffs
        the view's previous component snapshot against its new one and
        pops any dynamic-item pattern no longer present. This is reachable
        whenever a live view's tree stops carrying a
        ``DynamicPersistentButton`` class it used to; the ephemeral
        refresh-handoff timer swapping a view down to a single "Continue
        Session" button is the concrete case. Both mechanisms corrupt the
        same shared dict, and a fix for one is not a fix for the other.

        No-op, at debug, when none of the four bot-resolution tiers below
        yields a real ``discord.Client`` (a view with no interaction, no
        bot-bearing context, no restored ``_bot``, and no persistence
        manager) -- matching the no-op ``PersistenceManager`` already
        documents on the same shape. Silent here would leave a repair the
        rest of the codebase believes is in place quietly not running, and
        the resulting symptom is indistinguishable from the original defect.
        """
        if not _dynamic_button_classes:
            return
        # ``_bot`` covers a restored persistent view, which has no interaction
        # or context; the persistence manager's bot covers a view sent through
        # a bare channel context, which has no ``.bot``.
        bot = (
            getattr(self.interaction, "client", None)
            or getattr(self.context, "bot", None)
            or getattr(self, "_bot", None)
            or getattr(getattr(self.state_store, "persistence_manager", None), "_bot", None)
        )
        # Type-checked, not truthiness-checked: ``context``/``_bot`` are read
        # off whatever the caller supplied, and only a real Client exposes
        # add_dynamic_items with the expected contract.
        if isinstance(bot, discord.Client):
            bot.add_dynamic_items(*_dynamic_button_classes.values())
        else:
            logger.debug(
                f"{type(self).__name__} could not resolve a discord.Client to "
                f"re-drive the dynamic-item registry (interaction, context, "
                f"_bot, and the persistence manager each named no client, or "
                f"named something that isn't one); a DynamicPersistentButton "
                f"this view carried may stay unrouteable for other messages."
            )

    @_in_teardown_scope
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

        A view is torn down once. Called while another ``exit()`` or a
        timeout is closing the view, this waits for it, for up to 30
        seconds, after which that close carries this one out when it
        finishes. Called after one, it does only what that teardown left
        undone: it retires a registration the view still carries (a timeout
        keeps one), freezes a message nothing froze, and deletes the message
        when asked. Called while the view is being sent, the send carries
        the close out and ``send()`` returns ``None``: before the view has
        loaded, nothing is posted; after that, the send posts and then closes
        the message.

        Content another task passed to :meth:`refresh` while the view was
        being sent, or while a push or pop from it was in flight, still
        reaches a frozen message, under the frozen controls.

        Raises:
            RuntimeError: Called from inside a push or pop this view, or a
                view attached to it, takes part in: from the new view's
                ``on_load()`` or a ``rebuild=`` or ``nav_rebuild`` hook. Close
                the view once the navigation has returned.
        """
        await self._begin_exit("exit()")
        if delete_message is None:
            delete_message = self.exit_policy == "delete"
        await self._close(exit=True, message=_MESSAGE_DELETE if delete_message else _MESSAGE_FREEZE)
        return True

    async def _exit_body(self, delete_message: bool) -> None:
        """Tear the view down for ``exit()``, retiring its registration."""
        # Set again: a close cut off while this one waited resets it.
        self._closing = True
        # Exit tracked child views first. The registration goes only once the
        # children have closed, so an exit cut off before the view stops
        # leaves it registered as well as live.
        await self._cleanup_attached_children()
        await self._retire_registration()

        # Cancel the tasks this view owns. A freeze leaves its deliveries
        # running; a delete leaves them nothing to edit.
        self.task_manager._cancel_for_teardown(
            self.id, spare=() if delete_message else self._deliveries
        )

        # Stop this view (also repairs the dynamic-item registry, see stop())
        self.stop()

        # Unsubscribe before tearing down so the view's own subscriber does
        # not react to its own VIEW_DESTROYED.
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        # Before the message edit, which can take up to edit_timeout, so the
        # view's state row and registry entry are gone while it runs (the
        # unsubscribe above already freed its instance slot).
        await self.state_store._destroy_view(self.id, source_id=self.id)

        await self._close_message(delete_message)

    def _request_close(self, *, exit: bool, message: int) -> None:
        """Record a close for whoever holds this view's turn to carry out."""
        self._close_request = _merge_close(self._close_request, exit, message)

    def _leave_close(self, *, exit: bool, message: int) -> None:
        """Mark a recorded close as left with the turn's holder by another caller."""
        self._left_close = _merge_close(self._left_close, exit, message)

    async def _close(self, *, exit: bool, message: int) -> None:
        """Record a close and carry it out, or leave it with the turn's holder.

        ``exit`` retires the registration the view carries, and ``message``
        says how far its message closes. A view already torn down does only
        what the earlier teardown left undone: a registration a timeout kept,
        a freeze or a delete asked for since.
        """
        self._request_close(exit=exit, message=message)
        if not await self._take_close_turn():
            self._leave_close(exit=exit, message=message)
            return
        try:
            # Recorded again: a close cut off while this one waited dropped it.
            self._request_close(exit=exit, message=message)
            await self._carry_out_close()
        finally:
            self._release_lifecycle_turn()

    async def _close_stopped(self) -> None:
        """Tear down a view that stopped without a teardown, leaving its message as it is."""
        await self._close(exit=False, message=_MESSAGE_LEAVE)

    async def _carry_out_close(self) -> None:
        """Tear the view down as the recorded closes ask, then settle what they still owe.

        Run by the turn's holder. A close cut off before it tore anything
        down drops its own request, and the view is live again unless another
        close is still owed. The closes other callers left here, when the
        cut-off was a cancel, were not cancelled and carry on in a task of
        their own, and so does the view's own close once it has stopped,
        since it cannot take clicks again. That includes a view already torn
        down whose message edit was cut off: it may never have reached
        Discord, so the resumed close makes it again.
        """
        message_closed = self._message_closed
        reclaim_owed = self._reclaim_pending
        try:
            retired = False
            if not self._torn_down():
                exit_requested, message = self._close_request
                if exit_requested:
                    await self._exit_body(message == _MESSAGE_DELETE)
                    retired = True
                elif message == _MESSAGE_FREEZE:
                    await self._timeout_body()
                else:
                    await self._tear_down_stopped()
            elif self._still_registered() and not self._destroy_in_flight:
                # A teardown whose destroy was cut off or failed (a cancel
                # while a middleware awaited, a middleware that raised) leaves
                # the view registered after it unsubscribed; this close
                # finishes it rather than leaving a ghost. A destroy a
                # navigation is running is left to it.
                await self.state_store._destroy_view(self.id, source_id=self.id)
            # What the teardown finished stays finished if the settle is cut.
            message_closed, reclaim_owed = self._message_closed, self._reclaim_pending
            await self._settle_close_request(retired)
        except BaseException as e:
            left, self._left_close = self._left_close, None
            cancelled = isinstance(e, asyncio.CancelledError)
            own, self._close_request = self._close_request, None
            if cancelled and self.is_finished() and own is not None:
                left = _merge_close(left, *own)
                self._message_closed = message_closed
                # The resumed close redraws what this one's freeze had taken.
                self._reclaim_pending = self._reclaim_pending or reclaim_owed
            if cancelled and left is not None:
                self._resume_close(*left)
            else:
                self._end_closing_if_unowed()
            raise
        self._left_close = None

    def _close_waiting(self) -> bool:
        """Whether another close of this view waits for its turn."""
        return any(view is self for view in _CLOSE_WAITING.values())

    def _end_closing_if_unowed(self) -> None:
        """Hand a view a close was cut off on back, unless another close will finish it.

        The view takes clicks and navigation again and renders what it
        declined meanwhile. A close holding the turn, or waiting for it, or
        resumed and not yet started, finishes it instead.
        """
        holder = self._lifecycle_task
        if self._torn_down() or self._close_waiting():
            return
        if holder is not None and holder is not asyncio.current_task():
            return
        self._closing = False
        self._replay_if_owed()

    def _resume_close(self, exit: bool, message: int) -> None:
        """Carry out a close on a task of its own.

        For closes left with a holder that was cancelled, and for a view
        stopped while it was away for a navigation, whose teardown nothing
        else will run. Counted as waiting for the turn until it reaches it.
        """
        task = self.task_manager.create_task(
            _CLOSE_RESUME_TASK_OWNER, self._close(exit=exit, message=message)
        )
        _CLOSE_WAITING[task] = self
        task.add_done_callback(lambda done: _CLOSE_WAITING.pop(done, None))

    async def _take_close_turn(self) -> bool:
        """Take this view's turn for a close; ``False`` leaves the close with its holder.

        The holder carries out every recorded close before letting go. A
        close another task is running is waited out, so the caller returns
        once the view has closed, and one cut off hands the turn on. The
        close is left with the holder instead of waiting when the holder is a
        send, is this task, or waits (through other views' closes) on one
        this task holds, and after ``_CLOSE_WAIT_SECONDS``.
        """
        current = asyncio.current_task()
        # A resumed close counted as waiting until it got here.
        _CLOSE_WAITING.pop(current, None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CLOSE_WAIT_SECONDS
        while True:
            holder = self._lifecycle_task
            if holder is None:
                self._lifecycle_task = current
                self._lifecycle_idle.clear()
                return True
            if holder is current or self._lifecycle_sending or self._close_wait_cycle(current):
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    f"A close of {type(self).__name__} has waited {_CLOSE_WAIT_SECONDS:g}s "
                    f"for another close of it to finish, and leaves its request with that "
                    f"one to carry out. That close may be waiting on "
                    f"a slow Discord edit, or on this call (a hook or override awaits a "
                    f"task that closes this view), in which case the view closes once this "
                    f"call returns."
                )
                return False
            _CLOSE_WAITING[current] = self
            idle = asyncio.ensure_future(self._lifecycle_idle.wait())
            try:
                # Not wait_for: before Python 3.12 it can swallow a cancellation
                # that lands as the wait ends, and the close then runs anyway.
                await asyncio.wait({idle}, timeout=remaining)
            except asyncio.CancelledError:
                # A holder cut off meanwhile left the view to this close,
                # which is cut off too.
                _CLOSE_WAITING.pop(current, None)
                self._end_closing_if_unowed()
                raise
            finally:
                idle.cancel()
                _CLOSE_WAITING.pop(current, None)

    def _close_wait_cycle(self, current) -> bool:
        """Whether this view's turn holder waits, through other closes, on ``current``."""
        view, seen = self, set()
        while True:
            holder = view._lifecycle_task
            if holder is None:
                return False
            if holder is current:
                return True
            view = _CLOSE_WAITING.get(holder)
            if view is None or id(view) in seen:
                return False
            seen.add(id(view))

    def _release_lifecycle_turn(self) -> None:
        """Let go of this view's turn and wake the closes waiting for it."""
        self._lifecycle_task = None
        self._lifecycle_sending = False
        self._lifecycle_idle.set()

    async def _settle_close_request(self, retired: bool) -> None:
        """Carry out what recorded closes still owe once the view is torn down.

        ``retired`` says whether the teardown retired the registration. A
        timeout keeps it, so an ``exit()`` recorded alongside still retires
        it. The message closes as far as the furthest request asks, past what
        a teardown already did: a stopped child the parent's cleanup tore
        down is frozen by the timeout that stopped it. A close recorded while
        this runs is settled too. One cut off by a cancel puts back the request
        it had not finished, so the close that resumes it makes that edit
        again, retiring the registration again if that was where it stopped.
        """
        while self._close_request is not None:
            exit_requested, message = self._close_request
            self._close_request = None
            try:
                if exit_requested and not retired:
                    await self._retire_registration()
                    retired = True
                if message <= self._message_closed:
                    continue
                if exit_requested or message == _MESSAGE_DELETE:
                    await self._close_message(message == _MESSAGE_DELETE)
                else:
                    await self._freeze_for_timeout()
            except BaseException:
                self._close_request = _merge_close(self._close_request, exit_requested, message)
                raise

    async def _tear_down_stopped(self) -> None:
        """Tear down a view that stopped without a teardown, leaving its message."""
        # Its own children close with it: a timeout still to run finds the
        # view torn down and would not reach them.
        await self._cleanup_attached_children()
        self.task_manager._cancel_for_teardown(self.id, spare=self._deliveries)
        self.state_store._unsubscribe(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        await self.state_store._destroy_view(self.id, source_id=self.id)

    async def _close_message(self, delete_message: bool) -> None:
        """Delete the view's message, or freeze it, as an exit's last step."""
        self._message_closed = max(
            self._message_closed, _MESSAGE_DELETE if delete_message else _MESSAGE_FREEZE
        )
        if not delete_message:
            if not self._is_layout():
                # A render started during the wait lands after it and strips
                # the controls too, as the freeze below does.
                self._controls_removed = True
            # A render still in flight would land after the freeze and put
            # live-looking buttons back on a closed view's message.
            await self._wait_for_own_edits("Closing")
        if not self._message:
            return
        try:
            if delete_message:
                await self._bounded(self._message.delete())
            elif self._is_layout():
                # V2 messages ARE their components -- edit(view=None) would
                # produce an empty message (error 50006). Freeze instead.
                # _teardown_edit_target owns the freeze and picks what ships.
                content, redrawn = await self._reclaim_content()
                target = self._teardown_edit_target()
                if target is not None:
                    await self._ship_freeze(target, content, redrawn)
            else:
                content, _ = await self._reclaim_content()
                await self._bounded(self._message.edit(view=None, **content))
        except discord.NotFound:
            # Expected lifecycle: user dismissed the ephemeral, an
            # admin deleted the message, or the channel was deleted.
            # Nothing left to clean up on Discord's side.
            pass
        except asyncio.TimeoutError as e:
            # aiohttp's connect and socket timeouts are TimeoutErrors too and
            # fire below edit_timeout, so they get the transport message, not
            # a stall past a ceiling never reached.
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
                # An ephemeral past its 15-minute token cannot be edited: an
                # expected end, logged at DEBUG rather than as an error.
                logger.debug(
                    f"Exit cleanup skipped: ephemeral webhook token "
                    f"expired for {type(self).__name__}."
                )
            else:
                logger.error(f"Error cleaning up message: {e}")
        except Exception as e:
            logger.error(f"Error cleaning up message: {e}")

    async def _retire_registration(self) -> None:
        """Remove the persistent registrations this view's ``exit()`` closes.

        ``exit()`` calls it after the attached children have closed and before
        the view stops, and again when called on a view already torn down,
        so it removes only what is still registered. A view a persistent
        panel pushed to carries the panel's registration message id without
        its key, because navigation replaces the instance, and closing it
        closes the panel: the registration goes whether the exit freezes the
        message or deletes it, as the panel's own ``exit()`` would. A timeout
        does not reach here, so a view left idle keeps the panel restorable.

        Every registration naming the message goes, since a key renamed in
        place can leave two. The view's own key is skipped: a persistent view
        retires that in its override, which then calls this for a registration
        it carries under a different key.
        """
        await self._retire_carried_registrations(getattr(self, "_registry_message_id", None))

    async def _retire_registration_left_behind(self, posted) -> None:
        """Let go of the registration id for a message a re-send closed.

        The registrations carried for it were retired with the message; a
        persistent view also removes its own row, which still names it.
        """
        self._registry_message_id = None

    async def _retire_carried_registrations(self, carried: Optional[str]) -> None:
        """Remove the registrations naming message ``carried``, except the view's own key.

        Used when the view closes, and when a re-send moves it off that message.
        """
        if carried is None:
            return
        own = getattr(self, "_persistence_key", None)
        for key in self.state_store._persistence_keys_for_message(carried):
            if key == own:
                continue
            payload = ActionCreators.persistent_view_unregistered(key, message_id=carried)
            await self.dispatch("PERSISTENT_VIEW_UNREGISTERED", payload)
            # The panel that pushed here opted out of retiring its predecessor,
            # which its own exit() would now register again.
            if self._hand_back_record(key) is not None and key not in self.state_store.state.get(
                "persistent_views", {}
            ):
                await self._hand_back(key)

    def _channel_id_of(self, message) -> Optional[str]:
        """The id of the channel ``message`` was posted in, as a string.

        A channel type discord.py cannot build leaves a posted message
        without its channel; the interaction the send answered still names it.
        """
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        if channel_id is None:
            channel_id = getattr(self.interaction, "channel_id", None)
        return None if channel_id is None else str(channel_id)

    async def _move_carried_registrations(self, carried: Optional[str], message) -> None:
        """Move the registrations naming message ``carried`` to ``message``, except the view's own.

        A view a persistent panel pushed to carries the panel's registration
        without its key. Sent again, it takes the panel along: Back on the
        new message rebuilds the panel there, so a restart restores it there
        too. A persistent view whose own registration did not reach
        ``message`` moves nothing, and the send retires what it carries with
        the message it left.
        """
        if carried is None or message is None:
            return
        own = self._persistence_key
        keys = [
            key for key in self.state_store._persistence_keys_for_message(carried) if key != own
        ]
        channel_id = self._channel_id_of(message)
        if not keys or channel_id is None:
            return
        if own is not None and self._registry_message_id != str(message.id):
            return
        # Before the dispatch: the registry row is built from the view that
        # carries the registration's message.
        self._registry_message_id = str(message.id)
        registry = self.state_store.state.get("persistent_views", {})
        guild = getattr(message, "guild", None)
        for key in keys:
            entry = registry.get(key, {})
            payload = ActionCreators.persistent_view_registered(
                persistence_key=key,
                class_name=entry.get("class_name"),
                message_id=str(message.id),
                channel_id=channel_id,
                guild_id=str(guild.id) if guild else None,
                user_id=entry.get("user_id"),
            )
            await self.dispatch("PERSISTENT_VIEW_REGISTERED", payload)

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

    def _make_auto_exit_button(self, custom_id: str):
        """The Exit button ``auto_exit_button`` adds, marked as the library's own."""
        button = self.make_exit_button(custom_id=custom_id)
        button._cascadeui_auto_exit = True
        return button

    def _add_auto_exit_button(self, custom_id: str, row=None) -> None:
        """Add the Exit button a pattern's ``auto_exit_button`` asks for.

        Its id is fixed, like the pattern's own controls, so the button still
        answers after a restart on a persistent panel. A V1 row with no room
        left is refused naming the attribute, which the row error does not.
        """
        if row is None:
            self.add_exit_button(custom_id=custom_id)._cascadeui_auto_exit = True
            return
        try:
            self.add_exit_button(row=row, custom_id=custom_id)._cascadeui_auto_exit = True
        except ValueError as e:
            raise ValueError(
                f"{type(self).__name__}.auto_exit_button puts an Exit button on row "
                f"{row}, which has no room left ({e})."
                f"\n  Fix: leave a slot free on row {row} in _build_extra_items(), or set "
                f"auto_exit_button = False and place one with make_exit_button(row=...)."
            ) from e

    def _match_auto_exit(self, custom_id: str, *, row=None, extras: Optional[list] = None) -> None:
        """Add or remove the auto Exit button so it follows ``auto_exit_button``.

        Patterns call it at each render, so an override set on an instance
        takes effect at the next one. A V2 pattern passes ``extras``, the
        snapshot its recompose re-adds; otherwise the button sits in the tree.
        """
        items = self.children if extras is None else extras
        # Found by its marker: matched by id alone, a caller's own button
        # sharing the id would be taken for the Exit and removed.
        holder = next(
            (
                item
                for item in items
                if any(
                    getattr(inner, "custom_id", None) == custom_id
                    and getattr(inner, "_cascadeui_auto_exit", False) is True
                    for inner in (item.children if isinstance(item, ActionRow) else (item,))
                )
            ),
            None,
        )
        if self.auto_exit_button == (holder is not None):
            return
        if extras is not None:
            if holder is not None:
                extras.remove(holder)
            else:
                extras.append(ActionRow(self._make_auto_exit_button(custom_id)))
        elif holder is not None:
            self.remove_item(holder)
        else:
            self._add_auto_exit_button(custom_id, row=row)

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
