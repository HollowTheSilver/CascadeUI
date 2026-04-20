# // ========================================( Modules )======================================== // #


import asyncio

import discord

import logging

from ..components.base import StatefulButton
from ..state.actions import ActionCreators

logger = logging.getLogger(__name__)


# // ========================================( Mixin )======================================== // #


class _NavigationMixin:
    """Navigation and attachment machinery for stateful views.

    Houses the push/pop/replace engine, the back button helper, undo/redo
    dispatch wrappers, and the parent/child attachment cascade. Every
    entry point routes through ``_navigate_to`` so cleanup and kwarg
    forwarding stay consistent.

    Not a public class. ``_StatefulMixin`` inherits from this so the
    public ``StatefulView`` / ``StatefulLayoutView`` hierarchy is
    unchanged.
    """

    # // ==================( Navigation Stack )================== // #

    async def _navigate_to(
        self, view_class, interaction=None, *, action_type, action_payload, **kwargs
    ):
        """Internal: clean up current view, dispatch navigation action, construct next view.

        All navigation methods (push, replace, pop) share this path so cleanup
        and kwarg forwarding stay consistent in one place.
        """
        current_interaction = interaction or self.interaction

        # Version enforcement for push/pop -- these edit the same message, so
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

        # Stop this view and clean up its Python-level registration
        self.stop()
        self.state_store._unsubscribe(self.id)
        self.state_store._unregister_view(self.id)
        self.state_store._undo_enabled_views.pop(self.id, None)

        # Batch the full navigation sequence: NAVIGATION_* + SESSION_CREATED
        # (via _register_state) + VIEW_CREATED (via _register_state) +
        # VIEW_DESTROYED collapse into a single BATCH_COMPLETE. Reducers
        # still run inline so the ordering invariant (register new view
        # BEFORE destroying old) is preserved inside the batch.
        #
        # ``source_id`` is rebound to the new view's id after construction
        # (self has already unsubscribed above, so the OLD view won't
        # receive BATCH_COMPLETE -- the NEW view is the live subscriber
        # whose on_state_changed needs to ride the interaction ack cycle).
        async with self.state_store.batch() as batch:
            await self.state_store.dispatch(action_type, action_payload, source_id=self.id)

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

            # Rebind the batch source so BATCH_COMPLETE carries the new
            # view's id -- ``_notify_subscribers`` awards the inline
            # notification slot to the subscriber that actually needs to
            # refresh the reused message.
            batch.source_id = new_view.id

            # Push/pop reuse the same Discord message, so carry both message
            # references forward. Without this, on_state_changed() can't edit
            # the message (self.message would be None on the new view).
            if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP") and self._message:
                new_view._message = self._message
                new_view._webhook_message = self._webhook_message

            # Forward-transfer the navigation stack.  Push appends an entry
            # for the current view; pop strips the last entry.  Replace
            # starts fresh (one-way transition).
            if action_type == "NAVIGATION_PUSH":
                entry = {
                    "class_name": type(self)._class_session_key(),
                    "module": self.__class__.__module__,
                    "kwargs": self._init_kwargs if self._init_kwargs else {},
                }
                new_view._nav_stack = list(self._nav_stack) + [entry]
            elif action_type == "NAVIGATION_POP":
                new_view._nav_stack = list(self._nav_stack[:-1])

            # Propagate session origin so the entire navigation chain is tracked
            # under the root view's class name in the session index.
            if action_type == "NAVIGATION_REPLACE":
                # replace() is a one-way transition -- the destination view is independent
                # and should be tracked under its own class name, not the source's.
                new_view._instance_root_class = None
            else:
                origin = self._instance_root_class or type(self)._class_session_key()
                # If the destination IS the root class (e.g. pop() back to root),
                # clear the origin so it knows it's the root again.
                if view_class._class_session_key() == origin:
                    new_view._instance_root_class = None
                else:
                    new_view._instance_root_class = origin

            # Register the new view in the active view registry immediately.
            # Sub-views from push/pop typically edit the existing message instead
            # of calling send(), so register_view() must happen here -- otherwise
            # the sub-view is invisible to session limit enforcement.
            self.state_store._register_view(new_view)

            # Propagate participants for push/pop (same users, same message).
            # replace() is a one-way transition -- participants don't carry over.
            if action_type != "NAVIGATION_REPLACE" and self._participants:
                for pid in self._participants:
                    new_view._participants.add(pid)
                    self.state_store._register_participant(new_view, pid)

            # Register the new view in state BEFORE destroying the old one.
            # This keeps session["members"] non-empty during the transition
            # so the session is not prematurely deleted.
            await new_view._register_state()

            # Push/pop targets inherit the parent's message without routing
            # through _send_pipeline, so _update_message_state must fire
            # here for the new view's state row to carry message_id and
            # channel_id (otherwise the inspector shows None / None).
            if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP") and new_view._message:
                await new_view._update_message_state(new_view._message)

            # Forward-transfer undo/redo stacks through push/pop chain so the
            # undo timeline stays continuous across navigation.
            if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP"):
                old_view_state = self.state_store.state.get("views", {}).get(self.id, {})
                new_view_state = self.state_store.state.get("views", {}).get(new_view.id, {})
                if old_view_state.get("undo_stack"):
                    new_view_state["undo_stack"] = list(old_view_state["undo_stack"])
                if old_view_state.get("redo_stack"):
                    new_view_state["redo_stack"] = list(old_view_state["redo_stack"])

            # Now safe to remove the old view from state
            await self.state_store.dispatch(
                "VIEW_DESTROYED", ActionCreators.view_destroyed(self.id), source_id=self.id
            )

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
            class_name=type(self)._class_session_key(),
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
            if current_interaction:
                await self._safe_defer(current_interaction)
            result = rebuild(new_view)
            if asyncio.iscoroutine(result):
                result = await result
            edit_kwargs = result if isinstance(result, dict) else {}
            if current_interaction:
                try:
                    msg = await current_interaction.edit_original_response(
                        view=new_view, **edit_kwargs
                    )
                    new_view._message = msg
                except discord.HTTPException:
                    # Interaction token expired (15-min lifetime). Route the
                    # channel-endpoint fallback through refresh() so it picks
                    # up cooldown throttling and 429 backoff instead of
                    # racing with state-driven refreshes.
                    if new_view._message:
                        await new_view.refresh(**edit_kwargs)

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

        if not self._nav_stack:
            return None

        entry = self._nav_stack[-1]

        # Resolve the view class before navigating. Lazy import avoids a
        # circular import: base.py imports this module, and the registry
        # lives there alongside _register_view_class which __init_subclass__
        # uses at class-definition time.
        from .base import _view_class_registry

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
            if current_interaction:
                await self._safe_defer(current_interaction)
            result = rebuild(new_view)
            if asyncio.iscoroutine(result):
                result = await result
            edit_kwargs = result if isinstance(result, dict) else {}
            if current_interaction:
                try:
                    msg = await current_interaction.edit_original_response(
                        view=new_view, **edit_kwargs
                    )
                    new_view._message = msg
                except discord.HTTPException:
                    # Interaction token expired (15-min lifetime). Route the
                    # channel-endpoint fallback through refresh() so it picks
                    # up cooldown throttling and 429 backoff instead of
                    # racing with state-driven refreshes.
                    if new_view._message:
                        await new_view.refresh(**edit_kwargs)

        return new_view

    def _add_back_button(self):
        """Add a back button that pops the nav stack."""

        async def back_callback(interaction):
            await self._safe_defer(interaction)
            prev_view = await self.pop(interaction)
            if prev_view:
                try:
                    # Always edit via the interaction token so the deferred
                    # response is properly consumed by Discord
                    await interaction.edit_original_response(view=prev_view)
                except discord.HTTPException:
                    pass
            else:
                # Stack was empty -- pop() already stopped/unsubscribed this view,
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

    # // ==================( Undo/Redo )================== // #

    @property
    def undo_depth(self) -> int:
        """Number of snapshots currently on this view's undo stack."""
        views = self.state_store.state.get("views", {})
        return len(views.get(self.id, {}).get("undo_stack", []))

    @property
    def redo_depth(self) -> int:
        """Number of snapshots currently on this view's redo stack."""
        views = self.state_store.state.get("views", {})
        return len(views.get(self.id, {}).get("redo_stack", []))

    async def undo(self):
        """Undo the last state change for this view.

        Dispatches an UNDO action whose reducer pops the view's undo stack,
        pushes current application state to the redo stack, and restores
        the snapshot. All state changes happen inside the reducer pipeline.
        """
        # Pre-check: don't dispatch if stack is empty (avoids a no-op dispatch)
        views = self.state_store.state.get("views", {})
        view = views.get(self.id, {})
        if not view.get("undo_stack"):
            return

        await self.dispatch("UNDO", {"view_id": self.id, "session_id": self.session_id})

    async def redo(self):
        """Redo the last undone state change for this view.

        Dispatches a REDO action whose reducer pops the view's redo stack,
        pushes current application state to the undo stack, and restores
        the snapshot. All state changes happen inside the reducer pipeline.
        """
        # Pre-check: don't dispatch if stack is empty
        views = self.state_store.state.get("views", {})
        view = views.get(self.id, {})
        if not view.get("redo_stack"):
            return

        await self.dispatch("REDO", {"view_id": self.id, "session_id": self.session_id})

    # // ==================( Transitions )================== // #

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

    def _has_other_users_attached(self, requester_id) -> bool:
        """Check if any participant or attached child belongs to a different user."""
        if any(p != requester_id for p in self._participants):
            return True
        return any(
            c.user_id is not None and c.user_id != requester_id
            for c in self._attached_children
            if not c.is_finished()
        )

    def attach_child(self, child_view):
        """Register a child view for automatic cleanup on exit or timeout.

        When this view exits or times out, all attached children that
        haven't already finished are exited with ``delete_message=True``.

        Prefer the ``parent=`` kwarg on the child's constructor for the
        common case -- ``send()`` will call ``attach_child`` automatically
        on success. Use this method directly when attaching after send or
        when the child was not constructed with ``parent=``.

        Calling ``attach_child`` with a view that is already attached to
        this parent is a no-op. Calling it with a view attached to a
        *different* parent re-parents the child (removes it from the old
        parent's list first).

        Args:
            child_view: The child view to attach.
        """
        if child_view is self:
            raise ValueError("A view cannot attach itself as its own child")

        if child_view in self._attached_children:
            return

        # Detect circular chains: walk up from self to see if child_view
        # is already an ancestor. A->B->C->A would cause infinite
        # recursion during cleanup.
        ancestor = self._attached_to
        while ancestor is not None:
            if ancestor is child_view:
                raise ValueError(
                    f"Circular attachment: {type(child_view).__name__} is already "
                    f"an ancestor of {type(self).__name__}"
                )
            ancestor = ancestor._attached_to

        # Re-parent: detach from old parent if attached elsewhere
        old_parent = child_view._attached_to
        if old_parent is not None and old_parent is not self:
            try:
                old_parent._attached_children.remove(child_view)
            except ValueError:
                pass

        self._attached_children.append(child_view)
        child_view._attached_to = self

    async def _cleanup_attached_children(self):
        """Exit all tracked child views that are still alive.

        Finished entries are dropped silently. Stale references can accumulate
        across long-lived parents (e.g. a game view whose ephemeral fleet
        panels were refreshed via ``auto_refresh_ephemeral``); pruning here
        keeps the list bounded without requiring callers to manually untrack.

        The cascade is wrapped in ``store.batch()`` so N cascading
        ``VIEW_DESTROYED`` dispatches collapse into one ``BATCH_COMPLETE``
        notification. Subscribers see the final post-cleanup state once.

        No ``source_id`` is threaded: the parent is exiting (and has
        already unsubscribed), and every child unsubscribes before its
        own ``VIEW_DESTROYED`` dispatch. No subscriber in the fan-out
        owns the interaction ack cycle, so fire-and-forget is the right
        notification shape for this batch.
        """
        async with self.state_store.batch():
            for child in self._attached_children:
                if child is None:
                    continue
                if child.is_finished():
                    continue
                child._attached_to = None
                try:
                    await child.exit(delete_message=True)
                except Exception:
                    pass
        self._attached_children.clear()
