# // ========================================( Modules )======================================== // #


import asyncio
import functools
import time
import uuid
from typing import Any, Dict, Optional, Set

import discord
from discord import Interaction
from discord.ui import Item, View

from ..components.base import StatefulButton
from ..state.actions import ActionCreators
from ..state.singleton import get_store
from ..utils.errors import safe_execute, with_error_boundary

# Add logger
from ..utils.logging import AsyncLogger
from ..utils.tasks import get_task_manager

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


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
    }
)


def _register_view_class(cls):
    """Auto-register view classes for nav stack class resolution."""
    _view_class_registry[cls.__name__] = cls


# // ========================================( Exceptions )======================================== // #


class SessionLimitError(Exception):
    """Raised when send() is blocked by session_policy='reject'."""

    def __init__(self, view_type: str, limit: int):
        self.view_type = view_type
        self.limit = limit
        super().__init__(f"Session limit ({limit}) reached for {view_type}.")


# // ========================================( Mixin )======================================== // #


class _StatefulMixin:
    """View-agnostic state management shared by StatefulView (V1) and StatefulLayoutView (V2).

    This mixin contains all state integration, navigation, undo/redo, lifecycle,
    and session management logic. Concrete view classes combine it with either
    ``discord.ui.View`` (V1) or ``discord.ui.LayoutView`` (V2) and provide a
    version-specific ``send()`` method.
    """

    # Subclass config: state scoping ("user", "guild", or None)
    scope: Optional[str] = None

    # Subclass config: enable undo/redo support
    enable_undo: bool = False
    undo_limit: int = 20

    # Subclass config: auto-add a back button when pushed onto nav stack
    auto_back_button: bool = False

    # Subclass config: session limiting
    session_limit: Optional[int] = None  # None = unlimited
    session_scope: str = "user_guild"  # "user", "guild", "user_guild", "global"
    session_policy: str = "replace"  # "replace" or "reject"

    # Subclass config: interaction ownership
    owner_only: bool = True  # Reject interactions from non-owners
    owner_only_message: str = "You cannot interact with this."

    # Subclass config: auto-defer safety net
    auto_defer: bool = True
    auto_defer_delay: float = 2.5

    # Subclass config: interaction serialization
    # When True, rapid button clicks are processed one at a time to prevent
    # racing message edits that cause "This interaction failed" errors.
    serialize_interactions: bool = True

    # Persistent view marker — overridden to True by PersistentView / PersistentLayoutView
    _persistent: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _register_view_class(cls)

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

    def __init__(self, *args, **kwargs):
        # Extract custom arguments before passing to View/LayoutView
        self.state_store = kwargs.pop("state_store", None) or get_store()
        self.session_id = kwargs.pop("session_id", None)
        self.user_id = kwargs.pop("user_id", None)
        self.guild_id = kwargs.pop("guild_id", None)
        self.context = kwargs.pop("context", None)
        self.interaction = kwargs.pop("interaction", None)
        self.theme = kwargs.pop("theme", None)
        self._state_key = kwargs.pop("state_key", None)

        # Merge any kwargs auto-captured by the __init_subclass__ wrapper.
        # This includes all reconstructible kwargs from the most-derived
        # class, before any of them were consumed by intermediate __init__s.
        # For direct StatefulView usage (no wrapper), fall back to explicit
        # capture of the base class's own reconstructible kwargs.
        self._init_kwargs = getattr(self, "_pending_init_kwargs", {})
        if hasattr(self, "_pending_init_kwargs"):
            del self._pending_init_kwargs
        else:
            # No wrapper ran — StatefulView used directly, capture manually
            if self.theme is not None:
                self._init_kwargs["theme"] = self.theme
            if self._state_key is not None:
                self._init_kwargs["state_key"] = self._state_key

        # Initialize the discord.py base class (View or LayoutView)
        super().__init__(*args, **kwargs)

        # Unique identifier for this view instance
        self.id = str(uuid.uuid4())

        # Message reference
        self._message = None
        self._ephemeral = False

        # Whether state registration has been done
        self._registered = False

        # Session origin: when a view is pushed via navigation, this is set to
        # the root view's class name so the entire nav chain is tracked under
        # one session index key.  None means this view IS the root.
        self._session_origin: Optional[str] = None

        # Get task manager
        self.task_manager = get_task_manager()

        # Interaction serialization lock — prevents racing message edits
        # from rapid button clicks that cause "This interaction failed"
        self._interaction_lock = asyncio.Lock()

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

        if self.session_id is None and self.user_id is not None:
            # Include the class name so independent view hierarchies get
            # isolated sessions (separate nav stacks, undo history, etc.).
            # Pushed/popped views inherit session_id from their parent via
            # _navigate_to(), so the entire chain stays on one session.
            self.session_id = f"{type(self).__name__}:user_{self.user_id}"

        # Action types this view cares about — subclasses can override at
        # class level (e.g. subscribed_actions = {"MY_ACTION", ...})
        if "subscribed_actions" not in type(self).__dict__:
            self.subscribed_actions: Optional[Set[str]] = {
                "VIEW_DESTROYED",
                "SESSION_UPDATED",
            }

        # Build selector from the view's state_selector method (if overridden)
        selector = self._build_selector()

        # Subscribe to state updates with action filter and selector
        self.state_store.subscribe(
            self.id, self._on_state_changed, self.subscribed_actions, selector
        )

        # Register for undo tracking if this view has it enabled
        if self.enable_undo:
            self.state_store._undo_enabled_views[self.id] = self.undo_limit

    def create_task(self, coro):
        """Create a task owned by this view."""
        return self.task_manager.create_task(self.id, coro)

    @property
    def state_key(self) -> str:
        """Stable key for persistent state lookups.

        If a state_key was provided at init, it is used for all state
        data operations (reducer payloads, update_from_state lookups).
        Falls back to self.id for non-persistent views.
        """
        return self._state_key or self.id

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
        The view will only receive update_from_state() calls when the
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

        # Ensure session exists
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

    def get_theme(self):
        """Get the theme for this view, falling back to the global default.

        Returns a Theme instance. If no per-view theme is set and no global
        default exists, returns a bare Theme with standard defaults.
        """
        if self.theme is not None:
            return self.theme
        from ..theming.core import Theme, get_default_theme

        return get_default_theme() or Theme("fallback")

    # // ==================( Auto-Defer Safety Net )================== // #

    async def _scheduled_task(self, item: Item, interaction: Interaction):
        """Override discord.py's internal dispatch to add auto-defer and serialization.

        Replicates View._scheduled_task with two additions:

        1. **Auto-defer timer** — defers the interaction if the callback hasn't
           responded within ``auto_defer_delay`` seconds.
        2. **Interaction lock** — when ``serialize_interactions`` is True, rapid
           button clicks are processed one at a time. This prevents racing
           ``message.edit()`` calls that cause "This interaction failed" errors.
           The auto-defer timer runs *outside* the lock so queued interactions
           are deferred before the 3-second Discord timeout.
        """
        try:
            item._refresh_state(interaction, interaction.data)  # type: ignore

            allow = await item._run_checks(interaction) and await self.interaction_check(
                interaction
            )
            if not allow:
                return

            if self.timeout:
                self._BaseView__timeout_expiry = time.monotonic() + self.timeout  # type: ignore

            defer_task = None
            if self.auto_defer:
                defer_task = asyncio.create_task(self._auto_defer_timer(interaction))

            try:
                if self.serialize_interactions:
                    async with self._interaction_lock:
                        await item.callback(interaction)
                else:
                    await item.callback(interaction)
            finally:
                if defer_task is not None and not defer_task.done():
                    defer_task.cancel()
        except Exception as e:
            return await self.on_error(interaction, e, item)

    async def _auto_defer_timer(self, interaction: Interaction):
        """Background timer that defers the interaction if the callback hasn't responded."""
        try:
            await asyncio.sleep(self.auto_defer_delay)
            if not interaction.response.is_done():
                await interaction.response.defer()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(f"Auto-defer failed for interaction in {self.__class__.__name__}")

    # // ==================( Interaction Hooks )================== // #

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Called before every component callback to validate the interaction.

        When ``owner_only`` is True (the default), only the user who created
        the view can interact with it. Other users receive an ephemeral
        rejection message. Override this for custom access control (e.g.
        role-based checks), calling ``await super().interaction_check(interaction)``
        to preserve the ownership check.

        Skipped when ``self.user_id`` is None (e.g. restored PersistentViews
        with no originating user context).
        """
        if self.owner_only and self.user_id is not None:
            if interaction.user.id != self.user_id:
                try:
                    await interaction.response.send_message(self.owner_only_message, ephemeral=True)
                except discord.HTTPException:
                    pass
                return False
        return True

    async def on_error(self, interaction: Interaction, error: Exception, item: Item) -> None:
        """Called when a component callback raises an exception.

        Sends a generic ephemeral error embed to the user so the interaction
        doesn't silently fail. Subclasses can override this for custom handling.
        """
        logger.error(f"Error in {item!r} of view {self.__class__.__name__}: {error}", exc_info=True)

        embed = discord.Embed(
            title="Something went wrong",
            description="An unexpected error occurred while processing your interaction.",
            color=discord.Color.red(),
        )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    def _freeze_components(self):
        """Disable all interactive components in this view.

        V2 LayoutViews nest buttons inside ActionRow/Container, so we walk
        the full tree to reach them.  V1 Views have flat children.
        """
        items = self.walk_children() if self._is_layout() else self.children
        for item in items:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_timeout(self) -> None:
        """Called when the view times out. Disables all components and cleans up state."""
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
        self.state_store.unsubscribe(self.id)
        self.state_store.unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)
        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))

    async def _on_state_changed(self, state, action):
        """React to state changes."""
        logger.debug(f"View '{self.id}' received state update for action '{action['type']}'")

        # Default implementation - update UI if needed
        await self.update_from_state(state)

    async def update_from_state(self, state):
        """
        Update this view based on current state.

        This default implementation does nothing. Subclasses should override
        this method to implement state-driven UI updates when needed.

        Args:
            state: The current application state
        """
        pass

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

        Usage:
            async with self.batch() as batch:
                await batch.dispatch("SESSION_UPDATED", payload1)
                await batch.dispatch("NAVIGATION_REPLACE", payload2)
        """
        return self.state_store.batch()

    # // ========================================( Scoped State )======================================== // #

    @property
    def scoped_state(self) -> Dict[str, Any]:
        """Get the scoped state slice for this view based on its scope class var.

        Returns an empty dict if no scope is set or identifiers are missing.
        """
        if self.scope is None:
            return {}

        try:
            if self.scope == "user" and self.user_id is not None:
                return self.state_store.get_scoped("user", user_id=self.user_id)
            elif self.scope == "guild" and self.guild_id is not None:
                return self.state_store.get_scoped("guild", guild_id=self.guild_id)
        except ValueError:
            pass
        return {}

    async def dispatch_scoped(self, data: Dict[str, Any]) -> Any:
        """Dispatch a SCOPED_UPDATE action for this view's scope.

        Args:
            data: Dict of key-value pairs to merge into the scoped state.
        """
        if self.scope is None:
            raise ValueError("Cannot dispatch scoped update: view has no scope set")

        scope_id = None
        if self.scope == "user":
            scope_id = self.user_id
        elif self.scope == "guild":
            scope_id = self.guild_id

        if scope_id is None:
            raise ValueError(f"Cannot dispatch scoped update: no {self.scope}_id available")

        payload = {
            "scope": self.scope,
            "scope_id": scope_id,
            "data": data,
        }
        return await self.dispatch("SCOPED_UPDATE", payload)

    # // ========================================( Session Limiting )======================================== // #

    async def _enforce_session_limit(self):
        """Enforce session limiting before sending.

        Called by concrete ``send()`` implementations. Exits overflow views
        under replace policy, or raises ``SessionLimitError`` under reject policy.
        """
        if self.session_limit is None:
            return

        scope_key = self.state_store._build_session_scope_key(self)
        if scope_key is None:
            return

        view_type = self._session_origin or self.__class__.__name__
        existing = self.state_store.get_active_views(view_type, scope_key)
        overflow = len(existing) - self.session_limit + 1

        if overflow <= 0:
            return

        if self.session_policy == "reject":
            raise SessionLimitError(view_type, self.session_limit)

        # Pre-scan: check all candidates before exiting any, so we
        # don't destroy views and then raise on a later protected one
        to_replace = existing[:overflow]

        if not self._persistent:
            for old_view in to_replace:
                if getattr(old_view, "_persistent", False):
                    raise SessionLimitError(view_type, self.session_limit)

        # Replace policy: exit oldest views to make room
        for old_view in to_replace:
            await old_view.exit()

    # // ========================================( Navigation Stack )======================================== // #

    async def _navigate_to(
        self, view_class, interaction=None, *, action_type, action_payload, **kwargs
    ):
        """Internal: clean up current view, dispatch navigation action, construct next view.

        All navigation methods (push, replace, pop) share this path so cleanup
        and kwarg forwarding stay consistent in one place.
        """
        current_interaction = interaction or self.interaction

        # Version enforcement for push/pop — these edit the same message, so
        # crossing V1/V2 boundaries is forbidden (IS_COMPONENTS_V2 is one-way)
        if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP"):
            from discord.ui import LayoutView as _LayoutView

            self_is_v2 = isinstance(self, _LayoutView)
            target_is_v2 = issubclass(view_class, _LayoutView)
            if self_is_v2 != target_is_v2:
                source_ver = "V2 (LayoutView)" if self_is_v2 else "V1 (View)"
                target_ver = "V2 (LayoutView)" if target_is_v2 else "V1 (View)"
                raise TypeError(
                    f"Cannot push/pop between {source_ver} {self.__class__.__name__} "
                    f"and {target_ver} {view_class.__name__}. Navigation chains must "
                    f"use the same view version because the IS_COMPONENTS_V2 flag is "
                    f"one-way per message. Use replace() for cross-version transitions."
                )

        # Cancel background tasks owned by this view before stopping
        self.task_manager.cancel_tasks(self.id)

        # Stop this view and clean up its subscription before dispatching
        self.stop()
        self.state_store.unsubscribe(self.id)
        self.state_store.unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        await self.state_store.dispatch(action_type, action_payload, source_id=self.id)

        # Remove the departing view from state so it doesn't accumulate
        await self.state_store.dispatch(
            "VIEW_DESTROYED", ActionCreators.view_destroyed(self.id), source_id=self.id
        )

        # Pass through state store, session, and scoping context
        if "state_store" not in kwargs:
            kwargs["state_store"] = self.state_store
        if "session_id" not in kwargs:
            kwargs["session_id"] = self.session_id
        if "user_id" not in kwargs:
            kwargs["user_id"] = self.user_id
        if "guild_id" not in kwargs:
            kwargs["guild_id"] = self.guild_id

        # Create new view
        new_view = view_class(interaction=current_interaction, **kwargs)
        new_view._ephemeral = self._ephemeral

        # Push/pop reuse the same Discord message, so carry the message
        # reference forward. Without this, update_from_state() can't edit
        # the message (self.message would be None on the new view).
        if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP") and self._message:
            new_view._message = self._message

        # Propagate session origin so the entire navigation chain is tracked
        # under the root view's class name in the session index.
        if action_type == "NAVIGATION_REPLACE":
            # replace() is a one-way transition — the destination view is independent
            # and should be tracked under its own class name, not the source's.
            new_view._session_origin = None
        else:
            origin = self._session_origin or self.__class__.__name__
            # If the destination IS the root class (e.g. pop() back to root),
            # clear the origin so it knows it's the root again.
            if view_class.__name__ == origin:
                new_view._session_origin = None
            else:
                new_view._session_origin = origin

        # Register the new view in the active view registry immediately.
        # Sub-views from push/pop typically edit the existing message instead
        # of calling send(), so register_view() must happen here — otherwise
        # the sub-view is invisible to session limit enforcement.
        self.state_store.register_view(new_view)

        # Register the view in state (SESSION_CREATED + VIEW_CREATED) so
        # state["views"] contains this view's session_id. Without this,
        # features that look up session_id via state (e.g. UndoMiddleware)
        # won't work for pushed/popped views.
        await new_view._register_state()

        return new_view

    async def push(self, view_class, interaction=None, *, rebuild=None, **kwargs):
        """Push a new view onto the navigation stack.

        The current view's class is saved so pop() can reconstruct it later.
        Use this for drill-down UIs where the user needs a "back" path.

        Args:
            view_class: The StatefulView subclass to push to.
            interaction: Discord interaction for the new view.
            rebuild: Optional callable(view) to rebuild the new view's UI after
                construction. When provided, the interaction is auto-deferred
                and the message is edited with the rebuilt view. Accepts both
                sync and async callables. If the callable returns a dict, those
                are passed as extra kwargs to edit_original_response (e.g.
                ``{"embed": view.build_embed()}`` for V1 views).
            **kwargs: Additional kwargs passed to the new view constructor.
        """
        push_payload = ActionCreators.navigation_push(
            session_id=self.session_id,
            class_name=self.__class__.__name__,
            module=self.__class__.__module__,
            kwargs=self._init_kwargs if self._init_kwargs else None,
        )

        new_view = await self._navigate_to(
            view_class,
            interaction,
            action_type="NAVIGATION_PUSH",
            action_payload=push_payload,
            **kwargs,
        )

        # Add back button if the target view wants one
        if new_view.auto_back_button:
            new_view._add_back_button()

        if rebuild is not None:
            current_interaction = interaction or self.interaction
            if current_interaction and not current_interaction.response.is_done():
                await current_interaction.response.defer()
            result = rebuild(new_view)
            if asyncio.iscoroutine(result):
                result = await result
            edit_kwargs = result if isinstance(result, dict) else {}
            if current_interaction:
                msg = await current_interaction.edit_original_response(view=new_view, **edit_kwargs)
                new_view._message = msg

        return new_view

    async def pop(self, interaction=None, *, rebuild=None):
        """Pop the current view and return to the previous one on the nav stack.

        Returns the reconstructed previous view, or None if the stack is empty.

        Args:
            interaction: Discord interaction.
            rebuild: Optional callable(view) to rebuild the restored view's UI.
                When provided, the interaction is auto-deferred and the message
                is edited with the rebuilt view. Accepts both sync and async
                callables. If the callable returns a dict, those are passed as
                extra kwargs to edit_original_response (e.g.
                ``{"embed": view.build_embed()}`` for V1 views).
        """
        if not self.session_id:
            return None

        # Check if there's anything on the stack
        sessions = self.state_store.state.get("sessions", {})
        session = sessions.get(self.session_id, {})
        nav_stack = session.get("nav_stack", [])

        if not nav_stack:
            return None

        # Capture the entry BEFORE dispatching (reducer deepcopies, so our
        # reference is stale after dispatch — read first, dispatch second)
        entry = nav_stack[-1]

        # Resolve the view class before navigating
        class_name = entry.get("class_name")
        view_cls = _view_class_registry.get(class_name)

        if view_cls is None:
            logger.warning(f"Cannot pop: view class '{class_name}' not in registry")
            return None

        pop_payload = ActionCreators.navigation_pop(self.session_id)
        saved_kwargs = entry.get("kwargs") or {}

        new_view = await self._navigate_to(
            view_cls,
            interaction,
            action_type="NAVIGATION_POP",
            action_payload=pop_payload,
            **saved_kwargs,
        )

        if rebuild is not None:
            current_interaction = interaction or self.interaction
            if current_interaction and not current_interaction.response.is_done():
                await current_interaction.response.defer()
            result = rebuild(new_view)
            if asyncio.iscoroutine(result):
                result = await result
            edit_kwargs = result if isinstance(result, dict) else {}
            if current_interaction:
                msg = await current_interaction.edit_original_response(view=new_view, **edit_kwargs)
                new_view._message = msg

        return new_view

    def _add_back_button(self):
        """Add a back button that pops the nav stack."""

        async def back_callback(interaction):
            await interaction.response.defer()
            prev_view = await self.pop(interaction)
            if prev_view:
                try:
                    # Always edit via the interaction token so the deferred
                    # response is properly consumed by Discord
                    await interaction.edit_original_response(view=prev_view)
                except discord.HTTPException:
                    pass
            else:
                # Stack was empty — pop() already stopped/unsubscribed this view,
                # so remove the dead components from the message to avoid a broken UI
                try:
                    await interaction.edit_original_response(view=None)
                except discord.HTTPException:
                    pass

        self.add_item(
            StatefulButton(
                label="Back",
                style=discord.ButtonStyle.secondary,
                emoji="\u25c0",
                row=4,
                custom_id=f"nav_back_{self.id[:8]}",
                callback=back_callback,
            )
        )

    # // ========================================( Undo/Redo )======================================== // #

    async def undo(self, interaction=None):
        """Undo the last state change for this view's session.

        Dispatches an UNDO action whose reducer pops the undo stack,
        pushes current application state to the redo stack, and restores
        the snapshot. All state changes happen inside the reducer pipeline.
        """
        if not self.session_id:
            return

        # Pre-check: don't dispatch if stack is empty (avoids a no-op dispatch)
        sessions = self.state_store.state.get("sessions", {})
        session = sessions.get(self.session_id, {})
        if not session.get("undo_stack"):
            return

        await self.dispatch("UNDO", {"session_id": self.session_id})

    async def redo(self, interaction=None):
        """Redo the last undone state change for this view's session.

        Dispatches a REDO action whose reducer pops the redo stack,
        pushes current application state to the undo stack, and restores
        the snapshot. All state changes happen inside the reducer pipeline.
        """
        if not self.session_id:
            return

        # Pre-check: don't dispatch if stack is empty
        sessions = self.state_store.state.get("sessions", {})
        session = sessions.get(self.session_id, {})
        if not session.get("redo_stack"):
            return

        await self.dispatch("REDO", {"session_id": self.session_id})

    # // ========================================( Transitions )======================================== // #

    async def replace(self, view_class, interaction=None, **kwargs):
        """Replace the current view with a new one (no stack history saved).

        Use this for one-way transitions where going "back" doesn't apply,
        such as welcome screen -> main dashboard.
        """
        replace_payload = ActionCreators.navigation_replace(
            destination=view_class.__name__,
        )

        return await self._navigate_to(
            view_class,
            interaction,
            action_type="NAVIGATION_REPLACE",
            action_payload=replace_payload,
            **kwargs,
        )

    async def exit(self, delete_message=False):
        """Cleanly exit and clean up this view."""
        # Cancel all tasks owned by this view
        self.task_manager.cancel_tasks(self.id)

        # Stop this view
        self.stop()

        # Unsubscribe BEFORE dispatching VIEW_DESTROYED so the view's own
        # subscriber doesn't re-render after we remove components.
        self.state_store.unsubscribe(self.id)
        self.state_store.unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        # Clean up the message
        if self._message:
            try:
                if delete_message:
                    await self._message.delete()
                elif self._is_layout():
                    # V2 messages ARE their components — edit(view=None) would
                    # produce an empty message (error 50006).  Freeze instead.
                    self._freeze_components()
                    await self._message.edit(view=self)
                else:
                    await self._message.edit(view=None)
            except Exception as e:
                hint = ""
                if self._ephemeral:
                    hint = (
                        " This is likely because the interaction token expired (15-minute limit). "
                        "Ephemeral messages cannot be edited after the token expires."
                    )
                logger.error(f"Error cleaning up message: {e}.{hint}")

        # Dispatch view destroyed action (other subscribers still see this)
        await self.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(self.id))

        return True

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

        For PersistentView subclasses, pass a custom_id (e.g. ``custom_id="exit"``).
        """

        async def exit_callback(interaction):
            await interaction.response.defer()
            await self.exit(delete_message=delete_message)

        button = StatefulButton(
            label=label,
            style=style,
            row=row,
            emoji=emoji,
            custom_id=custom_id,
            callback=exit_callback,
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
        """Clean reference to ensure GC can collect this view."""
        if hasattr(self, "state_store") and hasattr(self, "id"):
            self.state_store.unsubscribe(self.id)
            self.state_store.unregister_view(self.id)


# // ========================================( Classes )======================================== // #


class StatefulView(_StatefulMixin, View):
    """Base class for all stateful V1 UI views."""

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False):
        """
        Send this view as a message using the stored context or interaction.

        This is the preferred way to display a StatefulView. It handles
        state registration and message tracking automatically.

        Args:
            content: Text content for the message.
            embed: A single embed to include.
            embeds: A list of embeds to include.
            ephemeral: Whether the message should be ephemeral (interaction only).
        """
        await self._enforce_session_limit()

        # Register in instance registry before state so the view is
        # tracked even if the state dispatch triggers subscribers that query it
        self.state_store.register_view(self)

        await self._register_state()

        if ephemeral:
            self._ephemeral = True
            if self.timeout is None:
                logger.warning(
                    f"{self.__class__.__name__}: ephemeral views with timeout=None will lose "
                    "editability after the interaction token expires (15 minutes). "
                    "Consider setting a finite timeout."
                )

        send_kwargs = {"view": self}
        if content is not None:
            send_kwargs["content"] = content
        if embed is not None:
            send_kwargs["embed"] = embed
        if embeds is not None:
            send_kwargs["embeds"] = embeds

        # Send the message, rolling back registry on failure to prevent
        # orphaned entries that permanently consume session limit slots
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
                    "StatefulView.send() requires either 'context' or 'interaction' to be set."
                )
        except Exception:
            self.state_store.unregister_view(self.id)
            raise

        self._message = message
        await self._update_message_state(message)
        return message
