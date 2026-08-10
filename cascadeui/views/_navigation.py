# // ========================================( Modules )======================================== // #


import asyncio
import logging

import discord

from ..components.base import StatefulButton
from ..state.actions import ActionCreators
from ..utils.responses import DISCORD_CALL_ERRORS, describe_discord_error

logger = logging.getLogger(__name__)

# Deduped per (source class, destination class) pair: the mismatch is a
# property of the stack shape, so one warning per edge is enough however
# many times a user walks it.
_exit_policy_mismatch_warned: set = set()


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

    @property
    def nav_depth(self) -> int:
        """Number of views beneath this one on the navigation stack.

        Zero on a view opened directly, one on the first push, and so on.
        Read it to decide whether a Back button belongs on the screen at
        all: the library disables one that has nowhere to go, but a screen
        reachable both by a push and by its own command usually wants it
        absent on the root entry rather than present and greyed::

            self.add_item(self.make_nav_row(back=bool(self.nav_depth)))

        A view that builds its nav row in more than one branch reads this
        in each of them, rather than tracking a separate root flag its
        constructor has to be told about.

        Safe to read inside ``on_load``: ``_navigate_to`` assigns the stack
        before it runs the destination's load hook, so the count is already
        correct by the time the tree is composed.
        """
        return len(self._nav_stack)

    async def _navigate_to(
        self,
        view_or_class,
        interaction=None,
        *,
        action_type,
        action_payload,
        defer_teardown=False,
        **kwargs,
    ):
        """Internal: clean up current view, dispatch navigation action, set up next view.

        All navigation methods (push, replace, pop) share this path so cleanup
        and kwarg forwarding stay consistent in one place.

        The first positional argument can be either a view class or a
        pre-constructed view instance. A class path constructs the view
        internally via ``view_class(**kwargs)``. An instance path uses
        the instance directly and rejects extra kwargs -- the instance
        is already built. Both paths run ``_register_view`` and
        ``_register_state`` here because ``__init__`` wires the
        subscriber and stores identity but does not dispatch
        SESSION_CREATED / VIEW_CREATED; only this method and
        ``_send_pipeline`` do.
        """
        is_instance = not isinstance(view_or_class, type)
        if is_instance:
            if kwargs:
                raise TypeError(
                    f"_navigate_to received a pre-constructed view instance "
                    f"({type(view_or_class).__name__}) but also extra kwargs "
                    f"{sorted(kwargs)}. Pre-constructed instances are already "
                    f"initialized; extra kwargs cannot be applied. Pass the "
                    f"class plus kwargs, or construct the instance with all "
                    f"required kwargs upfront."
                )
            new_view = view_or_class
            view_class = type(new_view)
        else:
            view_class = view_or_class
            new_view = None  # constructed inside the batch below

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

        # Quiesce the source: unsubscribe so it cannot race the destination
        # edit. The rest of the teardown -- stop(), undo de-registration, and
        # (in the batch below) VIEW_DESTROYED -- splits on ``defer_teardown``:
        #
        # - replace() and the default path commit it here and in the batch,
        #   the long-standing one-way behavior.
        # - push()/pop() defer it to a post-edit commit (_commit_source_teardown)
        #   so a failed destination edit can roll back to a live, re-clickable
        #   source instead of stranding a dead view behind a message that never
        #   swapped. The active-registry removal stays deferred to _destroy_view
        #   either way so it commits only after the VIEW_DESTROYED state
        #   removal lands.
        self.state_store._unsubscribe(self.id)
        if not defer_teardown:
            self.stop()
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
        # The source was quiesced before the batch opened (unsubscribed,
        # and for replace() stopped). An abort in here would leave it on
        # screen with live buttons that no longer render, and the
        # half-registered destination in both registries, so the recovery
        # the failed-edit path already owns runs for a raise too.
        async with self.state_store.batch() as batch:
            try:
                await self.state_store.dispatch(action_type, action_payload, source_id=self.id)

                if not is_instance:
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
                else:
                    # Pre-constructed instance: __init__ wired the subscriber
                    # and populated session_id / user_id / guild_id /
                    # state_store from whatever interaction or kwargs the
                    # caller passed. _register_state has not run yet (only
                    # _send_pipeline and this method dispatch it), so
                    # rebinding these fields here is safe -- nothing in
                    # state references the instance's auto-derived values.
                    #
                    # session_id rebinds to the parent's session so
                    # shared_data and the session lifecycle behave
                    # identically to the class path. Skipping this would
                    # destroy the parent's session when the parent is
                    # cleaned up (last-member rule), losing parent
                    # shared_data across navigation. user_id, guild_id,
                    # and state_store typically already match because the
                    # instance was constructed from the same interaction
                    # as the parent; rebind defensively for the
                    # cross-interaction edge case.
                    if not new_view._init_kwargs.get("session_id"):
                        new_view.session_id = self.session_id
                    if not new_view._init_kwargs.get("user_id"):
                        new_view.user_id = self.user_id
                    if not new_view._init_kwargs.get("guild_id"):
                        new_view.guild_id = self.guild_id
                    if not new_view._init_kwargs.get("state_store"):
                        new_view.state_store = self.state_store

                    # The class path constructs with the acting interaction; the
                    # instance path binds it here, or a later navigation from
                    # this view (the interaction-or-self.interaction fallback)
                    # degrades to the no-edit programmatic path.
                    if current_interaction is not None:
                        new_view.interaction = current_interaction

                new_view._ephemeral = self._ephemeral
                # Push and pop reuse one message, so a policy disagreement decides
                # the same message's teardown by depth. replace() sends a new
                # message and tears the source down under replace_policy, so a
                # differing policy there is coherent and not worth a warning.
                if action_type != "NAVIGATION_REPLACE":
                    self._warn_on_exit_policy_mismatch(new_view)

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
                    # Carry the ephemeral arming deadline, never recompute it: the
                    # webhook token belongs to the original send, so a mid-chain
                    # hop's handoff timer must sleep only the remainder of the
                    # original 900s window. The timer itself is scheduled
                    # post-commit in _settle_navigation -- armed here, it would
                    # orphan on rollback (_rollback_navigation never cancels the
                    # destination's tasks) and clobber the recovered source.
                    new_view._ephemeral_arm_deadline = self._ephemeral_arm_deadline

                # Forward-transfer the navigation stack.  Push appends an entry
                # for the current view; pop strips the last entry.  Replace
                # starts fresh (one-way transition).
                if action_type == "NAVIGATION_PUSH":
                    entry = {
                        "class_name": type(self)._class_session_key(),
                        "module": self.__class__.__module__,
                        "kwargs": self._init_kwargs if self._init_kwargs else {},
                        # Selection state the kwargs snapshot cannot carry: it was
                        # chosen after construction. pop() hands this back to
                        # restore_nav_state on the reconstruction. The entry is
                        # view-local and never serialized, so live objects are fine
                        # here. A raising override costs the restore, not the
                        # navigation -- the user still reaches the child view.
                        "view_state": self._capture_nav_state(),
                        # The library's own handoff resolution as this view holds
                        # it now. pop() hands it back so the restored view resumes
                        # the policy it had: inheritance flows down a chain, never
                        # back up it, and the declaration is a class attribute
                        # that rides the reconstruction on its own.
                        "refresh_handoff_resolved": self._refresh_handoff_resolved,
                    }
                    new_view._nav_stack = list(self._nav_stack) + [entry]
                elif action_type == "NAVIGATION_POP":
                    new_view._nav_stack = list(self._nav_stack[:-1])

                # Propagate session origin so the entire navigation chain is tracked
                # under the root view's class name in the instance index.
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
                # the sub-view is invisible to instance limit enforcement.
                # Pre-constructed instances also need this: __init__ wires the
                # subscriber and stores identity, but register_view fires only
                # from _send_pipeline or this navigation path.
                self.state_store._register_view(new_view)

                # Propagate participants for push/pop (same users, same message).
                # replace() is a one-way transition -- participants don't carry over.
                # The membership-check guard makes the propagation idempotent for
                # pre-constructed instances that already hold participants.
                if action_type != "NAVIGATION_REPLACE" and self._participants:
                    self._carry_participants_to(new_view)

                # Register the new view in state BEFORE destroying the old one.
                # This keeps session["members"] non-empty during the transition
                # so the session is not prematurely deleted. Both class and
                # instance paths reach this -- _register_state dispatches the
                # SESSION_CREATED + VIEW_CREATED actions that populate the
                # state row, and __init__ does not do this.
                await new_view._register_state()

                # Push/pop targets inherit the parent's message without routing
                # through _send_pipeline, so _update_message_state must fire
                # here for the new view's state row to carry message_id and
                # channel_id (otherwise the inspector shows None / None).
                if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP") and new_view._message:
                    await new_view._update_message_state(new_view._message)

                # Forward-transfer undo/redo stacks through push/pop chain so the
                # undo timeline stays continuous across navigation. Routed through a
                # VIEW_UPDATED dispatch so the transfer runs through the reducer like
                # every other state mutation, rather than writing into the live
                # state["views"] row in place.
                if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP"):
                    await self._carry_undo_stacks_to(new_view)

                # Remove the old view from state. _destroy_view drops its
                # active-registry entry once the state removal confirms, completing
                # the teardown deferred from the top of this method. push()/pop()
                # hold this until the destination edit confirms (see defer_teardown
                # above); _commit_source_teardown runs it on success.
                if not defer_teardown:
                    await self.state_store._destroy_view(self.id, source_id=self.id)
            except BaseException:
                # BaseException, not Exception: a cancellation mid-batch leaves
                # the source unsubscribed and the destination half-registered
                # exactly as a raise does.
                # INSIDE the batch deliberately, mirroring _send_pipeline. The
                # flush awards the inline notification slot to source_id
                # (rebound to the destination above) and awaits that
                # subscriber before any handler outside the block runs. A
                # rollback placed outside would therefore let the destination
                # paint itself onto the live message first, and only then be
                # unsubscribed and destroyed, leaving the user clicking a view
                # that no longer exists. Unsubscribing here lands before the
                # flush, so the aborted batch announces a net-zero sequence.
                await self._rollback_navigation(new_view)
                raise

        return new_view

    async def push(self, view_or_class, interaction=None, *, rebuild=None, **kwargs):
        """Push a new view onto the navigation stack.

        The current view's class is saved so pop() can reconstruct it later.
        Use this for drill-down UIs where the user needs a "back" path.

        ``view_or_class`` accepts either a view class (constructed
        internally with ``**kwargs``) or a pre-constructed view instance
        (used directly; ``**kwargs`` must be empty). The instance form
        unblocks views built by async classmethods like
        ``PaginatedLayoutView.from_data`` and ``from_cursor``, where
        the construction step happens before the navigation call.

        Args:
            view_or_class: A StatefulView subclass to construct, or a
                pre-constructed instance to use directly.
            interaction: Discord interaction for the new view.
            rebuild: Optional pre-edit hook ``callable(view)`` for views
                that need post-construction setup (V2 views that build
                empty and need ``v.build_ui()``; V1 views that need to
                return an ``embed`` / ``content`` dict for the edit).
                Accepts sync or async callables. When the callable
                returns a dict, its contents flow into
                ``edit_original_response`` as extra kwargs (e.g.
                ``{"embed": view.build_embed()}``). The Discord message
                is edited with the new view regardless of whether
                ``rebuild`` is supplied.
            **kwargs: Additional kwargs passed to the new view constructor.
                Must be empty when ``view_or_class`` is an instance.
        """
        push_payload = ActionCreators.navigation_push(
            session_id=self.session_id,
            class_name=type(self)._class_session_key(),
            module=self.__class__.__module__,
            kwargs=self._init_kwargs if self._init_kwargs else None,
        )

        new_view = await self._navigate_to(
            view_or_class,
            interaction,
            action_type="NAVIGATION_PUSH",
            action_payload=push_payload,
            defer_teardown=True,
            **kwargs,
        )

        # Add back button if the target view wants one
        if new_view.auto_back_button:
            new_view._add_back_button()

        await self._settle_navigation(new_view, interaction, rebuild)

        return new_view

    async def pop(self, interaction=None, *, rebuild=None):
        """Pop the current view and return to the previous one on the nav stack.

        Returns the reconstructed previous view, or None if the stack is empty.

        Args:
            interaction: Discord interaction.
            rebuild: Optional pre-edit hook ``callable(view)`` for the
                restored view. Same shape as ``push(rebuild=...)``: V2
                views run ``v.build_ui()``, V1 views return an
                ``embed`` / ``content`` dict for the edit. The message
                is edited with the restored view regardless of whether
                ``rebuild`` is supplied.
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
            defer_teardown=True,
            **saved_kwargs,
        )

        # Reapply the selection the parent held when it was pushed away from.
        # The kwargs above rebuild what the parent was CONSTRUCTED with; this
        # restores what it had SELECTED since. It runs before
        # _settle_navigation, so on_load (and any rebuild hook) reads the
        # restored value rather than the constructor's default.
        if new_view is not None:
            new_view._apply_nav_state(entry.get("view_state") or {})
            # Resume the handoff resolution this view held when it was
            # pushed away from. Reconstruction resets it to None and
            # _settle_navigation never inherits on pop, so without the
            # hand-back a derived engagement would be lost to the token
            # cliff -- and a view that held no resolution comes back
            # holding none.
            new_view._refresh_handoff_resolved = entry.get("refresh_handoff_resolved")

        await self._settle_navigation(new_view, interaction, rebuild, inherit_handoff=False)

        return new_view

    async def _apply_navigation_edit(self, new_view, interaction, rebuild) -> bool:
        """Run the optional rebuild hook, then edit the message to the new view.

        Called by ``_settle_navigation`` (the push()/pop() tail). Returns
        ``True`` when the destination reached the message and ``False`` when
        every edit endpoint failed -- ``_settle_navigation`` commits the
        deferred source teardown on ``True`` and rolls back to the live source
        on ``False``. The message edit happens whenever a current interaction
        is available, regardless of whether a ``rebuild`` callback was
        supplied -- the navigation contract is that the Discord message
        reflects the new view.

        ``rebuild`` is an optional pre-edit hook for callers whose
        views need post-construction setup (V2 views that build empty
        and need ``v.build_ui()``, V1 views that need to return an
        ``embed``/``content`` dict). When ``rebuild`` returns a dict,
        its contents flow into the edit as extra kwargs, limited to the
        ones every edit endpoint accepts (``content``, ``embed``,
        ``embeds``, ``attachments``, ``allowed_mentions``).

        The destination tree is built BEFORE the interaction is acked so
        the fast path can edit + ack in one round-trip via
        ``interaction.response.edit_message`` -- the same one-request shape the
        acting-view fast path in ``refresh()`` uses. A slow rebuild lets the
        auto-defer timer ack first
        (``is_done()`` becomes True), routing to the deferred edit path.
        """
        current_interaction = interaction or self.interaction
        if current_interaction is None:
            # No interaction to edit through (a programmatic push/pop). The
            # caller owns display; treat the state transition as committed so
            # the source teardown proceeds, matching the pre-deferral behavior.
            return True

        # Async preload on the destination view before the edit ships, so
        # navigating to a child (push) or back to a parent (pop) re-reads
        # its data source -- the reload-on-render contract. on_load()
        # defaults to a no-op; a view that overrides it no longer needs
        # rebuild=lambda v: v.load_and_build(). The explicit rebuild hook
        # below still runs for views that use it for post-construction
        # setup other than data loading.
        await new_view._run_on_load()

        # Fall back to the destination's own default rebuild. pop() passes no
        # rebuild (the back button is library code with nothing to hand it), so
        # a V1 destination names its own edit through nav_rebuild. A V2
        # destination leaves it None and ships components alone.
        if rebuild is None:
            rebuild = getattr(new_view, "nav_rebuild", None)

        edit_kwargs: dict = {}
        if rebuild is not None:
            result = rebuild(new_view)
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, dict):
                edit_kwargs = result
                # This dict reaches whichever of the three edit endpoints
                # the runtime picks, so it is held to the same portable set
                # refresh() enforces.
                new_view._reject_non_portable_edit_kwargs(edit_kwargs)

        # Pre-flight check on the new view's assembled tree. Catches the
        # same class of HTTP 400 the validator catches in send/refresh:
        # invalid placements introduced by a rebuild callback that
        # populates the tree post-init. Theme-managed accents resolve
        # first so the destination renders themed regardless of where
        # its tree was built.
        new_view._apply_theme_defaults()
        new_view._sync_back_buttons()
        new_view._check_placement()

        # Push/pop reuse the parent's Discord message, carried onto
        # new_view._message in the navigation batch above. That message --
        # not the acting interaction's -- is the navigation edit target.
        target_message = new_view._message or self._message

        # The acting interaction normally IS a click on target_message. A
        # with_confirmation wrapper, though, runs the original callback with
        # the confirm-button interaction, whose message is the ephemeral
        # prompt -- a different message. Both interaction-coupled paths below
        # (response.edit_message and edit_original_response) would then render
        # the new view onto the prompt and leave target_message stale. When
        # the interaction carries a message that differs from the view's,
        # edit target_message directly through the channel endpoint instead.
        # Interactions with no message (slash commands) keep the
        # edit_original_response path -- their original response IS the view's
        # message.
        interaction_msg_id = getattr(getattr(current_interaction, "message", None), "id", None)
        if (
            target_message is not None
            and interaction_msg_id is not None
            and interaction_msg_id != target_message.id
        ):
            # Ack the foreign interaction so the click does not error
            # (_safe_defer no-ops when a wrapper already consumed the slot),
            # then edit the view's own message. refresh() carries the
            # cooldown/backoff/digest machinery and its own acting-fast-path
            # gate re-confirms the message match.
            await self._safe_defer(current_interaction)
            if new_view._message is None:
                new_view._message = target_message
            try:
                await new_view.refresh(**edit_kwargs)
                if new_view._refresh_degraded:
                    # refresh() swallows a transport failure because a repaint
                    # can wait for the next state change. Navigation cannot:
                    # the source is torn down on the strength of this edit, so
                    # an edit that never reached Discord has to read as failure
                    # here or the swap completes with the old view on screen.
                    logger.warning(
                        f"Navigation edit did not reach Discord in "
                        f"{type(self).__name__}; rolling back to the source view."
                    )
                    return False
                return True
            except discord.HTTPException as e:
                # refresh() already absorbs the three conditions that leave
                # nothing to recover: NotFound, 429, and an expired
                # ephemeral's 401. A remaining HTTP error means the view's
                # own message could not be edited but the message is still
                # reachable, so report failure and let the navigation roll
                # back to the live source instead of stranding a dead view.
                logger.warning(
                    f"Navigation edit to the view's own message failed in "
                    f"{type(self).__name__}: status={getattr(e, 'status', '?')} "
                    f"code={getattr(e, 'code', '?')}; rolling back to source."
                )
                return False

        # The destination's mention rules ride the two direct edits below.
        # They stay out of edit_kwargs deliberately: the refresh() paths above
        # and below inject their own, and a non-empty kwargs dict there would
        # defeat refresh()'s digest short-circuit on every navigation.
        nav_mentions = new_view._resolve_allowed_mentions(None)
        direct_kwargs = dict(edit_kwargs)
        if nav_mentions is not None:
            direct_kwargs["allowed_mentions"] = nav_mentions

        # Fast path: a component/modal interaction whose response slot is still
        # open edits + acks in one request -- no separate defer round-trip, which
        # was the source of the navigation pause. Bounded at the ack deadline via
        # _ack_bounded (not edit_timeout) because the ack rides this call. Gated
        # on interaction type because edit_message silently no-ops for
        # slash-command interactions (e.g. a programmatic push() with no explicit
        # interaction, which falls back to self.interaction).
        fast_eligible = current_interaction.type in (
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        )
        if fast_eligible and not current_interaction.response.is_done():
            try:
                await self._ack_bounded(
                    current_interaction.response.edit_message(view=new_view, **direct_kwargs)
                )
                return True
            except asyncio.TimeoutError:
                # Ack window blown -- fall through to the deferred path (the
                # auto-defer timer has likely acked by now) to recover the edit
                # via the original-response endpoint.
                logger.warning(
                    f"Navigation fast path stalled in {type(self).__name__}; "
                    f"falling back to the deferred edit."
                )
            except discord.InteractionResponded:
                # The auto-defer timer (or another path) acked in the narrow
                # window between the is_done() guard above and edit_message's
                # own internal guard. InteractionResponded is a sibling of
                # HTTPException, not a subclass, so it would otherwise escape
                # the handler below. The slot is consumed -- fall through to the
                # deferred original-response edit, which shows the destination.
                logger.debug(
                    f"Navigation fast path raced an ack in {type(self).__name__}; "
                    f"using the deferred edit."
                )
            except DISCORD_CALL_ERRORS as e:
                # 429: stamp the backoff and report failure so the navigation
                # rolls back to the live source rather than hammering the rate
                # limit; the user re-clicks to retry. Other transient failures
                # fall through to the deferred path. RateLimited is a sibling
                # of HTTPException, not a subclass, so it is named explicitly.
                if self._handle_rate_limit(e):
                    return False

        # Deferred path: the interaction is already acked (auto-defer timer, a
        # caller pre-defer, or a fast-path fall-through). Edit through the
        # original-response endpoint.
        await self._safe_defer(current_interaction)
        try:
            msg = await self._bounded(
                current_interaction.edit_original_response(view=new_view, **direct_kwargs)
            )
            # Preserve the parent's plain Message ref. The edit response
            # is an InteractionMessage / WebhookMessage bound to the
            # 15-minute interaction token; subsequent edits need the
            # channel endpoint, which the plain ref provides.
            if new_view._message is None:
                new_view._message = msg
            return True
        except DISCORD_CALL_ERRORS:
            # Ordered above the timeout clause deliberately. aiohttp's connect
            # and socket timeouts inherit from both ClientError and
            # asyncio.TimeoutError, and discord.py builds its session with no
            # total timeout, so aiohttp's 30s sock_connect fires below
            # edit_timeout. Catching the timeout first would route a request
            # that never left the host past the one branch that recovers it.
            #
            # Interaction token expired (15-min lifetime), the ack was rate
            # limited, or the request never reached Discord. Route the
            # channel-endpoint fallback through refresh() so cooldown
            # throttling, 429 backoff, and the render-hash short-circuit
            # all participate in the edit.
            if new_view._message:
                try:
                    await new_view.refresh(**edit_kwargs)
                    if new_view._refresh_degraded:
                        # Same reasoning as the interaction path above: a
                        # swallowed transport failure is still a swap that
                        # did not happen.
                        logger.warning(
                            f"Navigation channel-endpoint edit did not reach "
                            f"Discord in {type(self).__name__}; rolling back."
                        )
                        return False
                    return True
                except discord.HTTPException as e:
                    # refresh() already absorbs NotFound, 429, and an expired
                    # ephemeral's 401; remaining HTTP errors mean the channel
                    # endpoint also failed on a message still worth retrying.
                    logger.warning(
                        f"Navigation channel-endpoint fallback failed in "
                        f"{type(self).__name__}: status={getattr(e, 'status', '?')} "
                        f"code={getattr(e, 'code', '?')}; rolling back to source."
                    )
            return False
        except asyncio.TimeoutError:
            logger.warning(
                f"Navigation edit stalled past {self.edit_timeout}s in "
                f"{type(self).__name__}; rolling back to source."
            )
            return False

    async def _settle_navigation(
        self, new_view, interaction, rebuild, *, inherit_handoff=True
    ) -> None:
        """Edit the message to the destination, then commit or roll back.

        Shared tail of push()/pop(). ``_apply_navigation_edit`` reports whether
        the destination reached the message. On success the source's attachment
        tracking hands off to the destination, the deferred source teardown
        commits, and the destination's ephemeral handoff timer (when the source
        carried an arming deadline) is scheduled. ``inherit_handoff`` gates the
        policy adoption inside that scheduling: ``True`` on push, where a
        destination with no answer of its own adopts the source's, and
        ``False`` on pop, where the restored view resumes the resolution
        ``pop()`` handed back from the nav-stack entry. On failure -- a
        contained edit error or a raising preload/rebuild -- the navigation
        rolls back to the live source view.
        """
        try:
            edited = await self._apply_navigation_edit(new_view, interaction, rebuild)
        except Exception:
            # A raising on_load()/rebuild()/placement check left the
            # destination unshowable. Recover the source, then re-raise so
            # the error still reaches on_error.
            await self._rollback_navigation(new_view)
            raise
        if edited:
            # Migrate attachment tracking onto the destination (same message,
            # same lifecycle) -- the same hand-off _reopen_ephemeral performs
            # for its refreshed instance. Without it, a navigating parent
            # orphans its attached children (the cleanup cascade dies with
            # the source) and a navigating child escapes its own parent's
            # cascade. The migration is destructive on the source's tracking,
            # so it waits for the confirmed edit: a rolled-back push must find
            # the source still holding its children and its parent link.
            # replace() never reaches this tail, so its one-way behavior
            # (attachments do not carry over) is preserved. Runs before the
            # teardown commit so the source is fully live during the hand-off.
            #
            # Snapshot: attach_child prunes the source's list as it
            # re-parents each child onto the destination.
            self._carry_attachments_to(new_view)

            await self._commit_source_teardown()
            # The source's arm timer was cancelled in _navigate_to and the
            # destination inherited the deadline with the message refs.
            # Scheduling waits until here, after the edit confirmed, because
            # a timer armed pre-edit would survive rollback and clobber the
            # recovered source. The carried deadline means the timer sleeps
            # only the time remaining to the 900s token cliff; the next hop's
            # cancel_tasks reaps this task, so the chain holds one live timer.
            #
            # Two separate questions gate the timer; conflating them would
            # read the deadline's mere presence as the policy itself. The
            # deadline answers WHEN: the message's token clock, present on
            # any ephemeral chain. The destination answers WHETHER, via
            # _refresh_handoff (see its property for the precedence).
            # Inheritance flows down the chain only. A push destination with
            # no answer of its own (no declaration and no resolution) adopts
            # the source's onto _refresh_handoff_resolved, so the next hop
            # and a rollback re-arm read a resolved policy rather than
            # presence. On pop the source is the departing descendant, so
            # nothing is adopted: pop() hands the restored view the
            # resolution it held when it was pushed away from, and a view
            # that held none does not acquire the child's.
            handoff = new_view._refresh_handoff
            if handoff is None and inherit_handoff:
                handoff = self._refresh_handoff
                if handoff is not None:
                    new_view._refresh_handoff_resolved = handoff
            if (
                handoff is not False
                and new_view._ephemeral_arm_deadline is not None
                and not new_view._refresh_armed
                and not new_view.is_finished()
            ):
                new_view.create_task(new_view._schedule_ephemeral_refresh())
        else:
            await self._rollback_navigation(new_view)

    async def _commit_source_teardown(self) -> None:
        """Complete the source view's teardown after a confirmed navigation edit.

        push()/pop() defer the source teardown past the destination edit (see
        ``defer_teardown`` in ``_navigate_to``). Once the edit confirms, the
        destination owns the message, so the source stops, drops its undo
        registration, and leaves state. The source was already unsubscribed
        during ``_navigate_to``.
        """
        self.stop()
        self.state_store._undo_enabled_views.pop(self.id, None)
        await self.state_store._destroy_view(self.id, source_id=self.id)

    async def _rollback_navigation(self, new_view=None) -> None:
        """Recover the source view after a failed navigation edit.

        The destination never reached the message, so its state registration is
        torn down and the source is re-subscribed. On the push/pop paths the
        source's teardown was deferred, so it was never stopped and comes back
        live and re-clickable on the message it still owns.

        ``replace()`` is the exception: it stops the source before the batch
        opens, which is its point of no return. A stopped view cannot be made
        clickable again (``StatefulButton`` skips dispatch once the view is
        finished), so re-subscribing one would only give it renders nobody can
        trigger. The subscription is restored for a live source only.

        ``new_view`` is ``None`` when the destination raised in its own
        ``__init__``: there is no registration to undo, and the source still
        needs its subscription back.
        """
        if new_view is not None:
            # _destroy_view clears the state row and the active registry but
            # not the subscriber __init__ wired up, and the subscriber holds a
            # strong reference to the view.
            self.state_store._unsubscribe(new_view.id)
            await self.state_store._destroy_view(new_view.id, source_id=new_view.id)
        if not self.is_finished():
            self.state_store.subscribe(
                self.id,
                self._handle_state_notification,
                self.subscribed_actions,
                self._build_selector(),
            )
        # _navigate_to cancelled the source's tasks ahead of the (now-failed)
        # teardown. Re-arm the ephemeral refresh handoff so a recovered
        # long-lived ephemeral source still swaps in its refresh button before
        # the 900s token cliff. The deadline is stamped for every ephemeral
        # send, so its presence only says the token clock is running --
        # whether this view engaged is _refresh_handoff's call, which is why
        # a pinned-off source comes back pinned off and a derived-declined
        # one comes back declined. The re-scheduled timer sleeps only the
        # remaining time to the original deadline.
        if (
            self._refresh_handoff is not False
            and self._ephemeral_arm_deadline is not None
            and not self._refresh_armed
            and not self.is_finished()
        ):
            self.create_task(self._schedule_ephemeral_refresh())

    async def _clear_on_empty_back(self, interaction) -> None:
        """Acknowledge a Back press that has nowhere to go.

        Called by the back-button callback when :meth:`pop` returns ``None``
        (empty stack), with the interaction still unacked. The panel is left
        as it is: the render seams disable a Back button whose stack is
        empty, so reaching this means the button was clicked before that
        state shipped, most plausibly on a persistent view restored after a
        restart with a Back button its rebuilt stack no longer justifies.
        Tearing the panel down there would destroy a working message on a
        press that asked for nothing.

        The interaction still has to be answered or the click reports as
        failed, so the response slot is consumed with a no-op deferred
        update. Override to close the panel on a Back press instead, though
        ``exit`` and the Exit button are the surfaces built for that.
        """
        try:
            if not interaction.response.is_done():
                await self._ack_bounded(interaction.response.defer())
        except asyncio.TimeoutError:
            logger.debug(
                f"Back-navigation ack stalled past {self.auto_defer_delay}s "
                f"in {type(self).__name__}."
            )
        except discord.InteractionResponded:
            # The auto-defer timer took the slot between the guard above and
            # the call. Already acked, which is the whole job here.
            pass
        except DISCORD_CALL_ERRORS as e:
            logger.debug(
                f"Back-navigation ack failed in {type(self).__name__}: "
                f"{describe_discord_error(e)}"
            )

    def _add_back_button(self):
        """Add a back button that pops the nav stack."""
        button = self.make_back_button(custom_id=f"nav_back_{self.id[:8]}", row=4)
        # Stash the item so paginated / tabbed / wizard rebuild paths that
        # call ``clear_items()`` can restore the navigation back button
        # after recomposing their own component tree.
        self._auto_back_item = button
        self.add_item(button)

    def _warn_on_exit_policy_mismatch(self, new_view) -> None:
        """Warn once when a navigation step changes ``exit_policy``.

        ``exit_policy`` is declared per class, but push and pop edit ONE
        message in place, so a stack whose screens disagree tears the same
        message down differently depending on how deep the user happened to
        go. Each class is individually valid, so nothing at class-definition
        time can see the disagreement; the stack is the only place it exists.

        A warning rather than a carried-forward value: a destination that
        deliberately differs (a confirmation screen that should delete while
        the hub freezes) is a legitimate shape, and silently overriding it
        would break that. Declaring the policy on a shared base is the fix
        when every depth should agree.
        """
        try:
            if self.exit_policy == new_view.exit_policy:
                return
            edge = (type(self).__name__, type(new_view).__name__)
            if edge in _exit_policy_mismatch_warned:
                return
            _exit_policy_mismatch_warned.add(edge)
            logger.warning(
                f"{edge[0]}.exit_policy={self.exit_policy!r} but "
                f"{edge[1]}.exit_policy={new_view.exit_policy!r}. Navigation "
                f"edits one message in place, so Exit behaves differently "
                f"depending on the depth the user reached. Declare the policy "
                f"on a shared base class when every screen should agree."
            )
        except AttributeError:
            # A view type without the attribute has nothing to disagree about.
            return

    def _restore_navigation_artifacts(self) -> None:
        """Re-add auto-added navigation items stripped by ``clear_items()``.

        Pattern rebuild paths (paginated page turns, tab switches, form
        re-layout, menu refresh, role panel rebuild) clear the view's
        children and recompose the tree from scratch. The auto back
        button injected by :meth:`_add_back_button` during ``push()``
        sits as a top-level child and would be lost on every rebuild
        without this restore step. Idempotent: a no-op when no back
        button is registered or when the rebuild path already re-added
        the item by other means.
        """
        back_item = getattr(self, "_auto_back_item", None)
        if back_item is not None and back_item not in self.children:
            self.add_item(back_item)

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

    async def replace(self, view_or_class, interaction=None, **kwargs):
        """Replace the current view with a new one (no stack history saved).

        Use this for one-way transitions where going "back" doesn't apply,
        such as welcome screen -> main dashboard.

        ``view_or_class`` accepts either a view class (constructed
        internally with ``**kwargs``) or a pre-constructed view instance
        (used directly; ``**kwargs`` must be empty). The instance form
        unblocks views built by async classmethods like
        ``PaginatedLayoutView.from_data`` and ``from_cursor``.
        """
        destination_class = (
            view_or_class if isinstance(view_or_class, type) else type(view_or_class)
        )
        replace_payload = ActionCreators.navigation_replace(
            destination=destination_class.__name__,
        )

        return await self._navigate_to(
            view_or_class,
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

    def _check_attachment(self, child_view) -> None:
        """Raise if attaching ``child_view`` would corrupt the parent chain.

        Split out of ``attach_child`` so the ``parent=`` kwarg can be
        checked before the Discord send rather than after it. ``send()``
        attaches once the message is live, and a rejection there would
        report a failure for a message that had already arrived.
        """
        if child_view is self:
            raise ValueError("A view cannot attach itself as its own child")

        # Detect circular chains: walk up from self to see if child_view
        # is already an ancestor. A->B->C->A would cause infinite
        # recursion during cleanup. The chain is collected on the way up
        # because two instances of one view class share a type name, and
        # the type alone can't distinguish the ancestor from the new child.
        ancestor = self._attached_to
        chain = [self]
        while ancestor is not None:
            if ancestor is child_view:
                chain.append(ancestor)
                path = " -> ".join(f"{type(v).__name__}({v.id})" for v in reversed(chain))
                raise ValueError(
                    f"Circular attachment: {type(child_view).__name__}({child_view.id}) "
                    f"is already an ancestor of {type(self).__name__}({self.id}). "
                    f"Chain: {path} -> {type(child_view).__name__}({child_view.id}). "
                    f"Fix: detach the existing link, or attach to a view outside this chain."
                )
            chain.append(ancestor)
            ancestor = ancestor._attached_to

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
        self._check_attachment(child_view)

        if child_view in self._attached_children:
            return

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
                except Exception as e:
                    # The list is cleared below either way, so a child that
                    # cannot tear itself down leaves nothing else to find
                    # it by. Every comparable teardown seam logs; this one
                    # was the exception.
                    logger.debug(
                        f"Attached child {type(child).__name__} failed to exit "
                        f"under {type(self).__name__}: {type(e).__name__}: {e}"
                    )
        self._attached_children.clear()
