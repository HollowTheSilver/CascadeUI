# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Optional

import discord

from ..state.actions import ActionCreators
from ..state.store import _CURRENT_INTERACTION
from ..utils.hooks import await_maybe
from ..utils.responses import DISCORD_CALL_ERRORS, _rewind_files, describe_discord_error
from ..utils.tasks import _in_teardown_scope

logger = logging.getLogger(__name__)

# Deduped per (source class, destination class) pair: the mismatch is a
# property of the stack shape, so one warning per edge is enough however
# many times a user walks it.
_exit_policy_mismatch_warned: set = set()

# The reload-turn label a navigation holds on its destination; base.py reads
# it to give a reload from a rebuild hook its own fix text.
_NAVIGATION_TURN = "push() or pop()"

# How a push or pop's edit ended. "unknown" is an edit that timed out or was
# cancelled after its request left, which Discord may or may not have applied.
_NAV_LANDED = "landed"
_NAV_FAILED = "failed"
_NAV_UNKNOWN = "unknown"

# A wait on a push or pop in flight that lasts this long is logged.
_NAVIGATION_WAIT_WARN_SECONDS = 30.0


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

        The whole sequence runs in one batch, so the navigation action and
        the destination's registration (and, for ``replace()``, the source's
        exit) notify once.
        The batch's source is the destination, which takes the inline
        notification: a push or pop destination records that render and
        plays it once it owns the message, and the source, still subscribed,
        records what it would have rendered.
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
            if new_view._used_before():
                # The navigation would take it off its own message, or bring
                # a closed view back: a cached instance pushed a second time
                # would tear the panel down onto a view that answers nothing.
                method = {"NAVIGATION_PUSH": "push()", "NAVIGATION_REPLACE": "replace()"}.get(
                    action_type, "The navigation"
                )
                raise RuntimeError(
                    f"{method} was given a {view_class.__name__} instance that has "
                    f"already been sent, pushed, or closed. A view instance goes on one "
                    f"message once. Fix: construct a new instance, or pass the class."
                )
        else:
            view_class = view_or_class
            new_view = None  # constructed inside the batch below

        from .base import _class_path

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

        if is_instance and defer_teardown:
            # Muted before it is carried the message: it is already
            # subscribed, and owns nothing until the navigation lands. Marked
            # after the checks above, so a refused push leaves the caller's
            # instance as it was.
            new_view._arriving_from = self
            self._navigation_destination = new_view

        # replace() closes the source only once the destination is registered,
        # so it refuses clicks and navigation from here: a push landing during
        # a registration that awaits would leave two views live.
        was_closing = self._closing
        if not defer_teardown:
            self._closing = True

        # Reducers still run inline in the batch, so the destination registers
        # before the source is destroyed. An abort would leave replace()'s
        # half-registered destination in both registries, so replace() discards
        # it inside the batch; push() and pop() settle in _navigate_in_place.
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
                    if defer_teardown:
                        new_view._arriving_from = self
                        self._navigation_destination = new_view
                else:
                    # Safe before _register_state: nothing in state names the
                    # instance's own identity yet. Without the source's session,
                    # the source's teardown would delete it (last member) and
                    # its shared_data with it.
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
                    # finds none and edits through the channel.
                    if current_interaction is not None:
                        new_view.interaction = current_interaction

                new_view._ephemeral = self._ephemeral
                # Push and pop reuse one message, so a policy disagreement decides
                # the same message's teardown by depth. replace()'s destination
                # sends a message of its own, so a differing policy there is
                # coherent and not worth a warning.
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
                    # A persistent panel's registration is owned by the message it
                    # registered under. Carried through non-persistent hops too, so
                    # a panel popped back to still exits as that registration's
                    # owner rather than removing whatever the key points at.
                    registry_message_id = getattr(self, "_registry_message_id", None)
                    if registry_message_id is not None:
                        new_view._registry_message_id = registry_message_id
                    hand_back = (
                        getattr(self, "_superseded_registration", None) or self._carried_hand_back
                    )
                    if hand_back is not None:
                        new_view._carried_hand_back = hand_back
                    # Carried, never recomputed: the webhook token belongs to the
                    # original send. The timer is scheduled by _commit_navigation;
                    # armed here, it would run on a destination the rollback discards.
                    new_view._ephemeral_arm_deadline = self._ephemeral_arm_deadline

                # Forward-transfer the navigation stack.  Push appends an entry
                # for the current view; pop strips the last entry.  Replace
                # starts fresh (one-way transition).
                if action_type == "NAVIGATION_PUSH":
                    entry = {
                        # The class path, not _class_session_key(): pop() resolves
                        # this against _view_class_registry, which is keyed on the
                        # import path, and a session key answers a different
                        # question -- two classes may deliberately share one.
                        "class_name": _class_path(type(self)),
                        "module": self.__class__.__module__,
                        "kwargs": self._init_kwargs if self._init_kwargs else {},
                        # Chosen after construction, so the kwargs cannot carry it.
                        # Never serialized, so live objects are fine here.
                        "view_state": self._capture_nav_state(),
                        # pop() restores the policy this view had: inheritance
                        # flows down a chain, never back up it.
                        "refresh_handoff_resolved": self._refresh_handoff_resolved,
                        # A render hook given to this instance rather than its
                        # class (a menu's fallback for a destination that
                        # names none), so the reconstruction renders the same.
                        "nav_rebuild": self.__dict__.get("nav_rebuild"),
                        "class_overrides": dict(self._class_overrides),
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
                    # The chain is also keyed by the root's scope: a screen with
                    # its own instance_scope would be filed where the root's
                    # limit never looks, letting one user hold two live panels.
                    origin_scope = self._instance_root_scope or self.instance_scope
                    # If the destination IS the root class (e.g. pop() back to root),
                    # clear the origin so it knows it's the root again.
                    if view_class._class_session_key() == origin:
                        new_view._instance_root_class = None
                    else:
                        new_view._instance_root_class = origin
                        new_view._instance_root_scope = origin_scope
                        new_view._instance_root_limit = self._root_instance_limit()

                # A push or pop destination never calls send(), so without this it
                # would be invisible to instance limits.
                self.state_store._register_view(new_view)

                # Propagate participants for push/pop (same users, same message).
                # replace() is a one-way transition -- participants don't carry over.
                # The membership-check guard makes the propagation idempotent for
                # pre-constructed instances that already hold participants.
                if action_type != "NAVIGATION_REPLACE" and self._participants:
                    self._carry_participants_to(new_view)

                # Before the source is destroyed, so the session always keeps a
                # member and is not deleted mid-transition.
                await new_view._register_state()

                # Push/pop targets inherit the parent's message without routing
                # through _send_pipeline, so _update_message_state must fire
                # here for the new view's state row to carry message_id and
                # channel_id (otherwise the inspector shows None / None).
                if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP") and new_view._message:
                    await new_view._update_message_state(new_view._message)

                # The undo timeline stays continuous across push and pop.
                if action_type in ("NAVIGATION_PUSH", "NAVIGATION_POP"):
                    await self._carry_undo_stacks_to(new_view)

                # replace() closes the source through exit() once the destination
                # is registered; push() and pop() settle it in _navigate_in_place.
                if not defer_teardown:
                    await self.exit()
            except BaseException:
                # A cancel leaves the destination half-registered as a raise does.
                # Inside the batch, since an abort still flushes the inline
                # notification this batch awards the destination: unsubscribed
                # first, it renders nothing for a navigation that failed.
                if not defer_teardown:
                    if new_view is not None:
                        self._discard_destination(new_view)
                        await self._destroy_settled(new_view, discarded=True)
                    self._closing = was_closing
                    if not was_closing:
                        self._replay_if_owed()
                raise

        if not defer_teardown:
            # A task this view owns that replaced it carries on as the
            # destination's, so the view it built can still be sent from it.
            self.task_manager._transfer(asyncio.current_task(), self.id, new_view.id)
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

        Raises:
            RuntimeError: This view already navigated away with ``push()``
                or ``pop()`` (its message belongs to the view that call
                returned, so navigate from that one), or has closed through
                ``exit()``, a timeout, or ``stop()``, or is closing in an
                ``exit()`` still running. Also raised when called
                from inside a push or pop this view takes part in: from the
                new view's ``on_load()`` or a rebuild hook, or from inside this
                view's own ``send()``, which has not finished with its message.
        """
        from .base import _class_path

        await self._wait_to_navigate("push()")
        self._refuse_navigation("push()")

        push_payload = ActionCreators.navigation_push(
            session_id=self.session_id,
            class_name=_class_path(type(self)),
            module=self.__class__.__module__,
            kwargs=self._init_kwargs if self._init_kwargs else None,
        )

        def build():
            return self._navigate_to(
                view_or_class,
                interaction,
                action_type="NAVIGATION_PUSH",
                action_payload=push_payload,
                defer_teardown=True,
                **kwargs,
            )

        def prepare(new_view):
            if new_view.auto_back_button:
                new_view._add_back_button()

        return await self._navigate_in_place(
            build, interaction, rebuild, prepare=prepare, inherit_handoff=True
        )

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

        Raises:
            RuntimeError: This view already navigated away with ``push()``
                or ``pop()`` (its message belongs to the view that call
                returned, so navigate from that one), or has closed through
                ``exit()``, a timeout, or ``stop()``, or is closing in an
                ``exit()`` still running. Also raised when called
                from inside a push or pop this view takes part in: from the
                new view's ``on_load()`` or a rebuild hook, or from inside this
                view's own ``send()``, which has not finished with its message.
        """
        await self._wait_to_navigate("pop()")
        self._refuse_navigation("pop()")

        if not self.session_id:
            return None

        if not self._nav_stack:
            return None

        return await self._pop_to(len(self._nav_stack) - 1, interaction, rebuild)

    async def _pop_to(self, depth: int, interaction, rebuild):
        """Return to the navigation entry at ``depth``, dropping every entry above it.

        ``pop()`` is ``depth = len(self._nav_stack) - 1``. The caller has
        waited out any navigation in flight and checked this view may navigate.
        """
        entry = self._nav_stack[depth]

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

        def build():
            return self._navigate_to(
                view_cls,
                interaction,
                action_type="NAVIGATION_POP",
                action_payload=pop_payload,
                defer_teardown=True,
                **saved_kwargs,
            )

        def prepare(new_view):
            # The navigation trimmed one entry; a return further back trims
            # to the entry it rebuilt.
            new_view._nav_stack = list(self._nav_stack[:depth])
            new_view._apply_class_overrides(entry.get("class_overrides") or {})
            # What the parent selected since it was constructed. Before the
            # edit, so on_load and a rebuild hook read the restored value.
            new_view._apply_nav_state(entry.get("view_state") or {})
            # Reconstruction resets the resolution and a pop never inherits, so
            # without this a derived engagement would be lost to the token cliff.
            new_view._refresh_handoff_resolved = entry.get("refresh_handoff_resolved")
            if entry.get("nav_rebuild") is not None:
                new_view.nav_rebuild = entry["nav_rebuild"]

        return await self._navigate_in_place(
            build, interaction, rebuild, prepare=prepare, inherit_handoff=False
        )

    async def _navigate_in_place(self, build, interaction, rebuild, *, prepare, inherit_handoff):
        """Run a push or pop: move this view's message to a new view, or keep it.

        A transaction on the message. While it runs this view is away: live,
        still subscribed, its tasks running, but nothing it does edits the
        message, and what it was asked to render is recorded. The new view
        arrives muted and owns nothing until its edit lands. The navigation
        ends in the ``finally`` below on every path, cancellation included,
        and settles everything other code waits on before it awaits anything:

        - landed: the message, the registration it carries, and the running
          task pass to the new view, and this view is torn down;
        - failed: the new view is discarded, and this view catches up on
          what it recorded;
        - unknown (the edit timed out or was cancelled after its request
          left): as failed, and this view re-renders, since the message may
          show the discarded view.

        ``build`` constructs and registers the new view; ``prepare`` runs on it
        before the edit. ``inherit_handoff`` lets a push destination adopt this
        view's refresh-handoff policy; a pop hands the restored view its own.
        """
        self._begin_navigation()
        outcome = _NAV_FAILED
        try:
            # An edit this view already started lands before this one, or the
            # older render would arrive last and cover the new view.
            await self._wait_for_own_edits()
            new_view = await build()
            prepare(new_view)
            outcome = await self._apply_navigation_edit(new_view, interaction, rebuild)
        except BaseException:
            if self._nav_edit_started:
                outcome = _NAV_UNKNOWN
            raise
        finally:
            new_view = self._navigation_destination
            self._navigation_destination = None
            if outcome == _NAV_LANDED:
                self._commit_navigation(new_view, inherit_handoff)
                loser = self
            else:
                self._roll_back_navigation(new_view, reclaim=outcome == _NAV_UNKNOWN)
                loser = new_view
            if loser is not None:
                await self._destroy_settled(loser, discarded=loser is not self)
        return new_view

    async def _apply_navigation_edit(self, new_view, interaction, rebuild) -> str:
        """Load, rebuild, and edit the message to ``new_view`` under its reload turn.

        A reload or load that another task starts on the destination (an
        avatar backfill its ``on_load`` schedules) waits for the navigation
        to land instead of interleaving with this ``on_load`` or painting
        the destination onto the message before this edit or a rollback.
        """
        async with new_view._reload_turn(_NAVIGATION_TURN):
            return await self._edit_to_destination(new_view, interaction, rebuild)

    async def _edit_to_destination(self, new_view, interaction, rebuild) -> str:
        """Run the optional rebuild hook, then edit the message to the new view.

        Called by ``_apply_navigation_edit``. Returns ``_NAV_LANDED`` when the
        destination reached the message, ``_NAV_FAILED`` when no endpoint took
        the edit, and ``_NAV_UNKNOWN`` when an edit stalled after its request
        left. The message edit happens on every push and pop, through the
        interaction when there is one and the channel endpoint otherwise,
        whether or not a ``rebuild`` callback was supplied: the navigation
        contract is that the Discord message reflects the new view.

        The message edited is the one the source is on, carried onto the
        destination, and the interaction's own endpoints edit it only when the
        interaction is a click on that message. The channel endpoint edits it
        instead when the interaction carries another message (the ephemeral
        prompt a ``with_confirmation`` click comes from), carries none (a slash
        command, or a modal opened from one, whose original response may be a
        different message from a view sent as a followup), or there is no
        interaction at all (a push or pop from a background task). A view never
        sent has no message, and nothing is edited.

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

        The destination's ``on_load()`` runs first, on every push and pop, so
        a view re-reads its data source whenever it is navigated to and needs
        no ``rebuild`` hook for loading. A ``rebuild`` hook runs after it, for
        setup other than data loading.
        """
        current_interaction = interaction or self.interaction

        await new_view._run_on_load()

        # Fall back to the destination's own default rebuild. pop() passes no
        # rebuild (the back button is library code with nothing to hand it), so
        # a V1 destination names its own edit through nav_rebuild. A V2
        # destination leaves it None and ships components alone.
        if rebuild is None:
            rebuild = getattr(new_view, "nav_rebuild", None)

        edit_kwargs: dict = {}
        if rebuild is not None:
            result = await await_maybe(rebuild(new_view))
            if isinstance(result, dict):
                edit_kwargs = result
                # This dict reaches whichever of the three edit endpoints
                # the runtime picks, so it is held to the same portable set
                # refresh() enforces.
                new_view._reject_non_portable_edit_kwargs(edit_kwargs)

        # The pre-flight check a send and a refresh run, on the finished tree,
        # after stabilizing ids and resolving theme accents as the send does.
        new_view._stabilize_custom_ids()
        new_view._apply_theme_defaults()
        new_view._sync_back_buttons()
        new_view._check_placement()
        # After on_load() and the rebuild hook, which can change the links the
        # commit carries: a link that would close a cycle rolls back here,
        # where raising at the commit would leave the navigation unsettled.
        self._check_hand_off(new_view)

        # Requests leave from here, so a raise or cancel past this point may
        # follow an edit Discord applied.
        self._nav_edit_started = True

        # The source's message, carried onto new_view in the navigation batch
        # above, is the edit target; the docstring says which endpoint edits it.
        target_message = new_view._message or self._message
        interaction_msg_id = getattr(getattr(current_interaction, "message", None), "id", None)
        if current_interaction is None and target_message is None:
            return _NAV_LANDED
        if interaction_msg_id is not None:
            foreign = target_message is not None and interaction_msg_id != target_message.id
        else:
            foreign = (
                target_message is not None
                and current_interaction is not None
                and current_interaction.type is not discord.InteractionType.component
            )
        if current_interaction is None or foreign:
            # The deferral keeps the click from failing; refresh() re-checks the
            # message match before its own fast path.
            if current_interaction is not None:
                await self._safe_defer(current_interaction)
            if new_view._message is None:
                new_view._message = target_message
            try:
                result = await new_view.refresh(**edit_kwargs)
            except discord.HTTPException as e:
                # refresh() absorbs NotFound, 429, and an expired ephemeral's 401.
                # Any other error leaves a reachable message, so the navigation
                # rolls back to the live source rather than strand a dead view.
                logger.warning(
                    f"Navigation edit to the view's own message failed in "
                    f"{type(self).__name__}: status={getattr(e, 'status', '?')} "
                    f"code={getattr(e, 'code', '?')}; rolling back to source."
                )
                return _NAV_FAILED
            return self._refresh_outcome(new_view, result, "Navigation edit")

        # The destination's mention rules ride the two direct edits below.
        # They stay out of edit_kwargs deliberately: the refresh() paths above
        # and below inject their own, and a non-empty kwargs dict there would
        # defeat refresh()'s digest short-circuit on every navigation.
        nav_mentions = new_view._resolve_allowed_mentions(None)
        direct_kwargs = dict(edit_kwargs)
        if nav_mentions is not None:
            direct_kwargs["allowed_mentions"] = nav_mentions
        # A file the hook returned may have gone out with an earlier edit.
        _rewind_files(direct_kwargs)

        # One request edits and acks, so it is bounded at the ack deadline, not
        # edit_timeout. Gated on type because edit_message does nothing for a
        # slash-command interaction.
        fast_eligible = current_interaction.type in (
            discord.InteractionType.component,
            discord.InteractionType.modal_submit,
        )
        if fast_eligible and not current_interaction.response.is_done():
            try:
                new_view._last_tree_digest = await self._ack_bounded(
                    new_view._edit_and_digest(
                        lambda: current_interaction.response.edit_message(
                            view=new_view, **direct_kwargs
                        )
                    )
                )
                new_view._has_rendered = True
                new_view._redrive_dynamic_items()
                return _NAV_LANDED
            except asyncio.TimeoutError:
                # Ack window blown -- fall through to the deferred path (the
                # auto-defer timer has likely acked by now) to recover the edit
                # via the original-response endpoint.
                logger.warning(
                    f"Navigation fast path stalled in {type(self).__name__}; "
                    f"falling back to the deferred edit."
                )
            except discord.InteractionResponded:
                # An ack landed between the is_done() check and edit_message's own
                # guard. InteractionResponded is not an HTTPException, so it is
                # caught by name; the deferred edit below ships the destination.
                logger.debug(
                    f"Navigation fast path raced an ack in {type(self).__name__}; "
                    f"using the deferred edit."
                )
            except DISCORD_CALL_ERRORS as e:
                # A 429 rolls back to the live source rather than retry against
                # the limit; other failures fall through to the deferred edit.
                if self._handle_rate_limit(e):
                    return _NAV_FAILED

        # Deferred path: the interaction is already acked (auto-defer timer, a
        # caller pre-defer, or a fast-path fall-through). Edit through the
        # original-response endpoint. A fast path that ran streamed any file
        # the edit carries.
        _rewind_files(direct_kwargs)
        await self._safe_defer(current_interaction)
        sent = {}

        async def edit_original():
            sent["message"] = await current_interaction.edit_original_response(
                view=new_view, **direct_kwargs
            )

        try:
            new_view._last_tree_digest = await self._bounded(
                new_view._edit_and_digest(edit_original)
            )
            new_view._has_rendered = True
            # Preserve the parent's plain Message ref. The edit response
            # is an InteractionMessage / WebhookMessage bound to the
            # 15-minute interaction token; subsequent edits need the
            # channel endpoint, which the plain ref provides.
            if new_view._message is None:
                new_view._message = sent["message"]
            new_view._redrive_dynamic_items()
            return _NAV_LANDED
        except DISCORD_CALL_ERRORS:
            # Above the timeout clause: aiohttp's connect timeouts are both
            # ClientError and asyncio.TimeoutError, and a request that never
            # left the host belongs here. An expired token, a rate-limited ack,
            # or a transport failure edits through the channel via refresh().
            if new_view._message:
                try:
                    result = await new_view.refresh(**edit_kwargs)
                except discord.HTTPException as e:
                    # refresh() already absorbs NotFound, 429, and an expired
                    # ephemeral's 401; remaining HTTP errors mean the channel
                    # endpoint also failed on a message still worth retrying.
                    logger.warning(
                        f"Navigation channel-endpoint fallback failed in "
                        f"{type(self).__name__}: status={getattr(e, 'status', '?')} "
                        f"code={getattr(e, 'code', '?')}; rolling back to source."
                    )
                    return _NAV_FAILED
                return self._refresh_outcome(new_view, result, "Navigation channel-endpoint edit")
            return _NAV_FAILED
        except asyncio.TimeoutError:
            logger.warning(
                f"Navigation edit stalled past {self.edit_timeout}s in "
                f"{type(self).__name__}; rolling back to source."
            )
            return _NAV_UNKNOWN

    def _refresh_outcome(self, new_view, result, where: str) -> str:
        """Read a navigation edit made through ``refresh()`` as an outcome.

        ``refresh()`` absorbs failures a repaint can wait out, and navigation
        cannot: the source is torn down on the strength of this edit. A
        transport failure never reached Discord, a stall may have, and a
        deferral under a rate limit sent nothing. A missing message still
        commits: there is nothing left to show the source on.
        """
        if result == "dropped":
            if new_view._refresh_degraded:
                logger.warning(
                    f"{where} did not reach Discord in {type(self).__name__}; "
                    f"rolling back to the source view."
                )
                return _NAV_FAILED
            logger.warning(
                f"{where} stalled in {type(self).__name__}; rolling back to the source view."
            )
            return _NAV_UNKNOWN
        if result == "deferred":
            return _NAV_FAILED
        return _NAV_LANDED

    def _begin_navigation(self) -> None:
        """Mark this view away for a push or pop that is starting."""
        self._away_for_navigation = True
        self._missed_while_away = False
        self._nav_edit_started = False
        self._navigation_task = asyncio.current_task()
        self._navigation_settled.clear()

    def _end_navigation(self) -> None:
        """Lift the away state and release every caller waiting on it."""
        self._away_for_navigation = False
        self._navigation_task = None
        self._navigation_settled.set()

    def _navigation_in_flight(self):
        """The view running a push or pop this view takes part in, or ``None``.

        This view when it is the source, the view it arrives from when it is
        the destination.
        """
        if self._away_for_navigation:
            return self
        return self._arriving_from

    def _used_before(self) -> bool:
        """Whether this instance was already sent, pushed, or closed.

        A view instance goes on one message once: placing a used one again
        (a cached instance handed to ``push()``, ``replace()``, or returned by
        ``build_reopen_view()``) would take it off its own message or bring a
        closed view back.
        """
        return (
            self._message is not None
            or self._lifecycle_task is not None
            or self._closed()
            or self._torn_down()
            or self._navigation_in_flight() is not None
        )

    async def _wait_out_navigation(self, method: str) -> None:
        """Wait until no push or pop this view takes part in is in flight.

        An ``exit()``, a timeout, a click, a parent's cleanup, or another
        navigation would otherwise act on a message about to change hands, or
        on a destination that may yet be discarded. Every waiter is woken when
        a navigation settles and one of them can start the next, so each
        re-checks before it proceeds. A wait that lasts
        ``_NAVIGATION_WAIT_WARN_SECONDS`` is logged, since one that the
        navigation is itself waiting on never ends.

        Raises:
            RuntimeError: The call comes from inside the navigation it would
                wait for (the new view's ``on_load`` or a rebuild hook), which
                would act on a message the navigation has not handed over.
        """
        source = self._navigation_in_flight()
        if source is None:
            return
        if source._navigation_task is asyncio.current_task():
            self._raise_inside_navigation(method, source)
        watchdog = asyncio.get_running_loop().call_later(
            _NAVIGATION_WAIT_WARN_SECONDS, self._warn_long_navigation_wait, method
        )
        try:
            while source is not None:
                await source._navigation_settled.wait()
                source = self._navigation_in_flight()
        finally:
            watchdog.cancel()

    async def _wait_to_navigate(self, method: str) -> None:
        """Wait until no send of this view, push or pop it takes part in, or Continue on it runs.

        A click reaches a view once its message is posted, while the send is
        still fetching the message back and attaching the view to its
        parent. A push or pop run then would move the message out from
        under the send, which would report a view already torn down as sent.
        A Continue wins as it does over a close: a navigation from code that
        lands meanwhile finds the panel handed on to the replacement.

        Raises:
            RuntimeError: The call comes from inside the send or the
                navigation it would wait for.
        """
        while True:
            await self._wait_out_send(method)
            await self._wait_out_navigation(method)
            await self._wait_out_reopen(method)
            reopening = (
                self._reopen_task is not None and self._reopen_task is not asyncio.current_task()
            )
            if not self._lifecycle_sending and not reopening:
                return

    async def _wait_out_send(self, method: str) -> None:
        """Wait for a send of this view in another task; raise inside the send itself."""
        if not self._lifecycle_sending:
            return
        if self._lifecycle_task is asyncio.current_task():
            raise RuntimeError(
                f"{method} on {type(self).__name__} was called inside its own send(): "
                f"from its on_pre_send(), on_load(), seed_initial_state(), or a hook the "
                f"send ran. The send has not finished with the view's message. Fix: "
                f"navigate once send() has returned."
            )
        watchdog = asyncio.get_running_loop().call_later(
            _NAVIGATION_WAIT_WARN_SECONDS, self._warn_long_send_wait, method
        )
        try:
            await self._send_finished()
        finally:
            watchdog.cancel()

    def _warn_long_send_wait(self, method: str) -> None:
        logger.warning(
            f"{method} on {type(self).__name__} has waited "
            f"{_NAVIGATION_WAIT_WARN_SECONDS:g}s for the view's send() to finish. If "
            f"the send is waiting on this call (a hook it runs awaits a task that "
            f"navigates this view), the wait never ends."
        )

    def _raise_inside_navigation(self, method: str, source) -> None:
        role = "from it" if source is self else "bringing it in"
        raise RuntimeError(
            f"{method} on {type(self).__name__} was called inside the push() or pop() "
            f"{role}, by the new view's on_load() or a rebuild= or nav_rebuild hook. "
            f"That navigation has not yet decided which view owns the message. Fix: "
            f"decide before calling push() or pop(), or act on the view it returns "
            f"once it has returned."
        )

    async def _begin_exit(self, method: str) -> None:
        """Start an ``exit()``: wait out a navigation, then mark the view closing.

        A view closing takes no more clicks or navigation while its children
        close. Refused first, before anything is closed, when the exit would
        cascade into an attached view whose push or pop runs in this task
        (the parent's ``exit()`` called from that navigation's new view):
        the cascade would reach it only after this view had begun closing,
        and could not wait on the navigation it is part of.
        """
        await self._wait_out_navigation(method)
        await self._wait_out_reopen(method)
        # After the waits, so a push or Continue that lands meanwhile is caught.
        self._warn_if_handed_over(method)
        self._refuse_cascade_into_own_navigation("A parent's exit()")
        self._closing = True

    def _refuse_cascade_into_own_navigation(self, method: str) -> None:
        """Raise when a cascade from this view would reach a navigation in this task.

        Checked before anything closes, so a refused call leaves every
        attached view as it was.
        """
        current = asyncio.current_task()
        pending = list(self._attached_children)
        seen = set()
        while pending:
            view = pending.pop()
            if id(view) in seen:
                continue
            seen.add(id(view))
            source = view._navigation_in_flight()
            if source is not None and source._navigation_task is current:
                view._raise_inside_navigation(method, source)
            pending.extend(view._attached_children)
            if view._successor is not None:
                pending.append(view._successor)

    def _warn_long_navigation_wait(self, method: str) -> None:
        logger.warning(
            f"{method} on {type(self).__name__} has waited "
            f"{_NAVIGATION_WAIT_WARN_SECONDS:g}s for a push() or pop() it takes part in "
            f"to finish. If that navigation is waiting on this call (the new view's "
            f"on_load or a rebuild hook awaits a task that exits, navigates, or "
            f"clicks this view), the wait never ends."
        )

    async def _wait_for_own_edits(
        self,
        action: str = "A push() or pop() from",
        timeout: Optional[float] = None,
        *,
        until_idle: bool = False,
    ) -> bool:
        """Wait for edits this view started before an edit that must land after them.

        A push or pop edits the message to the new view, and a close freezes
        it; an older edit landing last would cover either. Only the edits in
        flight when the wait begins are waited for: one started later ships
        what the push or close has already decided (an away view's render
        only records, a closed view's ships its controls disabled), and
        waiting for those too never ends while the view keeps editing.
        ``until_idle`` also waits for edits started meanwhile, for a caller
        whose edit nothing later accounts for. Each edit is bounded by
        ``edit_timeout``; a wait with no bound that lasts
        ``_NAVIGATION_WAIT_WARN_SECONDS`` is logged, naming ``action``.
        Returns ``False`` when ``timeout`` seconds pass with an edit still in
        flight.
        """
        pending = set(self._edits_pending)
        if not pending:
            return True
        loop = asyncio.get_running_loop()
        watchdog = loop.call_later(_NAVIGATION_WAIT_WARN_SECONDS, self._warn_long_edit_wait, action)
        give_up = None if timeout is None else loop.time() + timeout
        try:
            while pending:
                remaining = None if give_up is None else give_up - loop.time()
                if remaining is not None and remaining <= 0:
                    return False
                _, pending = await asyncio.wait(pending, timeout=remaining)
                if not pending and until_idle:
                    # Another task can start an edit between the last one
                    # landing and this task running.
                    pending = set(self._edits_pending)
            return True
        finally:
            watchdog.cancel()

    def _warn_long_edit_wait(self, action: str) -> None:
        logger.warning(
            f"{action} {type(self).__name__} has waited {_NAVIGATION_WAIT_WARN_SECONDS:g}s "
            f"for an edit the view started earlier to reach Discord, and edits the message "
            f"after it; with edit_timeout = None that edit has no bound."
        )

    def _last_successor(self):
        """The view this one's panel passed to, following each hand-off, or this view.

        A hand-off is a ``push()`` or ``pop()``, an ephemeral panel's
        Continue button, or a restart in the same process that restored the
        panel.
        """
        view = self
        while view._successor is not None:
            view = view._successor
        return view

    def _warn_if_handed_over(self, method: str) -> None:
        """Log once when a call that would do nothing reaches a view that handed its panel on.

        The panel is another view's now, so a ``refresh()`` or ``exit()`` on
        it changes nothing on screen. Code that keeps a reference to a panel
        otherwise finds its calls stop working, with nothing said, as soon as
        a user clicks into another screen. Silent for a view whose own
        timeout fired while a push was landing, since its ``on_timeout()``
        runs on the view it belonged to, and for an exit library cleanup
        drives, which follows the push to the view that took over.
        """
        if (
            self._successor is None
            or self._warned_handed_over
            or self._timed_out
            or self._library_exits
        ):
            return
        self._warned_handed_over = True
        logger.warning(
            f"{type(self).__name__}.{method} was called on a view that handed its panel "
            f"on (a push() or pop() from it, an ephemeral panel's Continue button, or a "
            f"restart that restored the panel), so "
            f"it did nothing: the panel now shows {type(self._last_successor()).__name__}. "
            f"Call it on current_view to reach that view."
        )

    @property
    def current_view(self):
        """The view now showing this view's panel.

        This view, or the view it handed the panel to, following each
        hand-off in turn: a ``push()`` or ``pop()``, the Continue button of
        an ephemeral panel, whose replacement goes on a new message, or a
        restart in the same process that restored the panel. Code
        that keeps a reference to a panel reaches the screen now on it
        through this: the library returns the new view only to the callback
        that ran the hand-off, and the old view's ``exit()`` and navigation
        calls no longer reach the panel.
        """
        return self._last_successor()

    async def _exit_or_successor(self, delete_message=None) -> bool:
        """Exit this view, or the view it handed its panel to.

        For library cleanup of a view it collected before awaiting: an
        instance-limit replacement, a persistent retire, a parent's cascade,
        the devtools exits. ``exit()`` on a view that navigated away does
        nothing, since a caller still holding it (its own timeout, code right
        after the push) refers to the view that handed its message on. A
        navigation the exit waits out can land, so the view that took over is
        exited in turn. ``delete_message`` may be a callable that picks the
        value for each view exited. Returns whether a view exited.
        """
        view = self
        while True:
            view = view._last_successor()
            if view._torn_down():
                return False
            choice = delete_message(view) if callable(delete_message) else delete_message
            # Marks the view, not the context, so tasks the exit spawns and
            # later calls from user code still warn. A count, since a Continue
            # on the view can be running a library exit of its own.
            view._library_exits += 1
            try:
                await view.exit(delete_message=choice)
            finally:
                view._library_exits -= 1
            if view._successor is None:
                return True

    def _refuse_handed_over(self, method: str) -> None:
        """Raise when this view does not hold its panel.

        Either another view took the panel over, or this view is the
        destination a failed push or pop discarded, and the panel is still
        on the view the navigation started from.
        """
        name = type(self).__name__
        if self._discarded:
            raise RuntimeError(
                f"{name}.{method} was called on the view a failed push() or pop() "
                f"returned. The navigation rolled back, so the panel is still on the "
                f"view it started from. Fix: navigate from that view."
            )
        if self._successor is None:
            return
        if self._timed_out:
            # The on_timeout() of a view whose timer fired while a push was
            # landing: the panel is live on the new view, whose own timer runs.
            fix = (
                f"the panel is live on {type(self._last_successor()).__name__}, which "
                f"keeps its own timeout, so there is nothing to replace. Check "
                f"current_view is self before navigating from on_timeout()."
            )
        else:
            fix = "navigate from that view instead."
        concurrent = (
            " Two clicks handled at once reach this when both navigate, since "
            "serialize_interactions = False runs their callbacks side by side; "
            "set it to True to queue them."
            if not self.serialize_interactions
            else ""
        )
        raise RuntimeError(
            f"{name}.{method} was called on a view that already handed its panel on "
            f"(a push() or pop() from it, an ephemeral panel's Continue button, or a restart "
            f"that restored the panel). "
            f"The panel belongs to the view current_view gives. Fix: {fix}{concurrent}"
        )

    def _refuse_navigation(self, method: str) -> None:
        """Raise when this view no longer owns a message a push or pop could move."""
        self._refuse_handed_over(method)
        if self._closed():
            raise RuntimeError(
                f"{type(self).__name__}.{method} was called on a view that has closed "
                f"(exit(), a timeout, or stop()) or is closing in an exit() or replace() "
                f"still running. Navigating would bring its message back to life under a "
                f"new view. Fix: navigate before closing it, or send a new view."
            )

    def _commit_navigation(self, new_view, inherit_handoff: bool) -> None:
        """Hand this view's message and place to ``new_view``, and tear this view down.

        Synchronous, so everything a waiting caller reads is settled before
        the caller runs. The destination takes the message, the registration
        it carries, this view's parent and child links, and the running task,
        so code after ``push()`` or ``pop()`` in a task this view owned runs
        on. This view's other tasks are cancelled. Kept, the message would let
        this view edit it after the swap: a ``refresh()`` would paint its
        stopped tree over the destination, a V1 ``exit()`` would strip the
        destination's buttons, and a persistent ``exit()`` would remove the
        registration the destination carries. Its state row is destroyed by
        the caller.

        The destination's refresh-handoff timer starts here, once its edit
        has landed, and sleeps only what is left of the carried deadline; the
        next hop's commit cancels it, so a chain holds one live timer. The
        deadline says when, the destination's ``_refresh_handoff`` says
        whether: a push destination with no policy of its own adopts this
        view's, and a pop destination adopts nothing, since ``pop()`` hands
        the restored view the resolution it held.
        """
        self._carry_attachments_to(new_view)
        self._settle_undo_carry(new_view)
        self._message = None
        self._webhook_message = None
        self._registry_message_id = None
        self._successor = new_view
        self._end_navigation()
        self.state_store._unsubscribe(self.id)
        self.task_manager._transfer(asyncio.current_task(), self.id, new_view.id)
        self.task_manager._cancel_for_teardown(self.id)
        self.stop()
        # Stopping removed this view's custom_ids from the message's routing,
        # including any the destination shares.
        new_view._reregister_dispatch()
        self.state_store._undo_enabled_views.pop(self.id, None)
        # A render the destination recorded while arriving replays from its
        # turn's release, which ran just before this with nothing awaited
        # between, so the replay starts after the mute is lifted.
        new_view._arriving_from = None
        # Armed only now: a timer started before the edit landed would outlive
        # a rollback and paint over the source.
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
            new_view._schedule_ephemeral_refresh()

    def _roll_back_navigation(self, new_view, *, reclaim: bool) -> None:
        """Discard ``new_view`` and return this view to the message it kept.

        Synchronous for the same reason as the commit. This view was never
        stopped or unsubscribed, so it is live at once; it renders once if it
        recorded a render while away, and re-arms its refresh handoff if the
        timer came due meanwhile. With ``reclaim`` the message may show the
        discarded view, so this view re-renders even with nothing recorded.
        ``new_view`` is ``None`` when the destination was never built.
        """
        if new_view is not None:
            self._discard_destination(new_view)
        self._end_navigation()
        # Before another task can render: the message stays as this view's
        # last render left it, and refreshes held meanwhile come after that.
        self._take_held_rules(self._mention_rules[1])
        missed = self._missed_while_away
        self._missed_while_away = False
        if self.is_finished():
            # Stopped while it was away: by its timer, whose teardown, waiting
            # on this navigation, runs next, or by user code, which closes it
            # here as a send closes a view stopped during it. The freeze edit
            # is the redraw, and ships even with nothing to disable once the
            # baseline no longer claims the message shows this view.
            self._arm_after_rollback = False
            if reclaim:
                self._last_tree_digest = None
                self._reclaim_pending = True
            if missed:
                # Content held while it was away ships under the frozen controls.
                self._deliver_later(self._replay_render())
            if not self._timed_out and not self._torn_down():
                from .base import _MESSAGE_FREEZE

                self._resume_close(True, _MESSAGE_FREEZE)
            return
        if reclaim:
            self._last_tree_digest = None
            self._reclaim_pending = True
            self._deliver_later(self._reclaim_message(render=missed))
        elif missed:
            self._deliver_later(self._replay_render())
        # A timer that came due while this view was away stood down; the one
        # scheduled again waits only the time left. Every ephemeral send stamps
        # a deadline, so _refresh_handoff decides whether this view engaged.
        arm = self._arm_after_rollback
        self._arm_after_rollback = False
        if (
            arm
            and self._refresh_handoff is not False
            and self._ephemeral_arm_deadline is not None
            and not self._refresh_armed
        ):
            self._schedule_ephemeral_refresh()

    def _discard_destination(self, new_view) -> None:
        """Take down a navigation destination that never reached the message.

        ``push()`` / ``pop()`` still return it, and it carries this view's
        message and registration id from the navigation batch, so both are
        unbound first: a later ``exit()`` on it can neither edit the message
        this view kept nor remove the registration this view still owns. Its
        tasks (an avatar backfill its ``on_load`` started) are cancelled,
        sparing the running task, which may be one it was handed. Stopped, so
        a reload on it answers that no message remains, and unsubscribed,
        since ``_destroy_view`` clears the state row and the active registry
        but not the subscriber, which holds a strong reference to the view.
        Its state row is destroyed by the caller.
        """
        new_view._message = None
        new_view._webhook_message = None
        new_view._registry_message_id = None
        new_view._arriving_from = None
        new_view._discarded = True
        new_view.task_manager._cancel_for_teardown(new_view.id, keep_caller=True)
        new_view.stop()
        self.state_store._unsubscribe(new_view.id)
        self.state_store._undo_enabled_views.pop(new_view.id, None)

    async def _destroy_settled(self, view, *, discarded: bool) -> None:
        """Destroy the state row of the view a navigation settled against.

        A ``discarded`` destination also closes its children (a companion
        panel its ``on_load()`` sent with ``parent=``), as its ``exit()``
        would have; a source that handed its message over passed its
        children on at the commit. Shielded: this runs after the settle,
        often while a cancellation is propagating, and a second cancel would
        otherwise stop the removal part way and leave the row behind.

        While it runs, a close of that view leaves the row for it to remove.
        The mark is set before the first suspension: a close waiting on this
        navigation wakes at the settle, ahead of the shielded task's first
        step.
        """
        view._destroy_in_flight = True

        async def destroy():
            try:
                if discarded:
                    await view._cleanup_attached_children()
                await self.state_store._destroy_view(view.id, source_id=view.id)
            finally:
                view._destroy_in_flight = False

        await asyncio.shield(destroy())

    async def _reclaim_message(self, *, render: bool) -> None:
        """Put this view back on a message a failed navigation may have changed.

        With ``render`` the view also missed renders while away, and they run
        first, as a rollback's replay runs them. The content its
        ``nav_rebuild`` names (the content ``pop()`` restores a V1 view with)
        then ships with its tree, since those renders can leave the message
        carrying the discarded view's embed. An armed view skips them and
        ships its tree as it stands, the refresh button, which is put back
        after ``nav_rebuild`` in case the hook rebuilt the tree.

        A close that begins first and freezes the message runs the hook
        itself, and the freeze carries the content; one undone runs this
        again once the view is live. A teardown that leaves the message as it
        is, or deletes it, runs neither.
        """
        _CURRENT_INTERACTION.set(None)
        armed = self._refresh_armed
        if render and not armed:
            # Without a missed render, a deferred state render is a turn's to
            # replay at its release.
            origin, self._deferred_origin = self._deferred_origin, 0
            held, self._held = self._held, {}
            await self._run_deferred(origin, held)
        if self._closing or self._torn_down():
            self._reclaim_declined = True
            return
        self._reclaim_pending = False
        try:
            kwargs = {}
            nav_rebuild = getattr(self, "nav_rebuild", None)
            if nav_rebuild is not None:
                frozen = list(self.children) if armed else None
                result = await await_maybe(nav_rebuild(self))
                if frozen is not None:
                    self.clear_items()
                    for item in frozen:
                        self.add_item(item)
                if isinstance(result, dict):
                    kwargs = result
            if not kwargs and self._deferred_origin:
                # A render deferred behind a reload ships it at that reload's
                # release; one already shipped is skipped by refresh().
                return
            await self.refresh(**kwargs)
        except Exception as e:
            # DEBUG when the bot closed meanwhile (no session to send through)
            # or a close tore the view down while its hook ran (the hook may read
            # what the close cleared, such as the parent link).
            closed = self._closed_session(e)
            logger.log(
                logging.DEBUG if closed or self._torn_down() else logging.ERROR,
                f"Re-rendering {type(self).__name__} after a failed navigation raised: {e}",
                exc_info=not closed,
            )

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
        handed = self._successor is not None
        # A batch still collecting this view's steps adds its step at its exit.
        await self.state_store._wait_for_undo_batches(self.id)
        if not handed and self._successor is not None:
            # The panel moved on during the wait, taking the history with it.
            return await self._last_successor().undo()
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
        handed = self._successor is not None
        await self.state_store._wait_for_undo_batches(self.id)
        if not handed and self._successor is not None:
            return await self._last_successor().redo()
        # Pre-check: don't dispatch if stack is empty
        views = self.state_store.state.get("views", {})
        view = views.get(self.id, {})
        if not view.get("redo_stack"):
            return

        await self.dispatch("REDO", {"view_id": self.id, "session_id": self.session_id})

    # // ==================( Transitions )================== // #

    @_in_teardown_scope
    async def replace(self, view_or_class, interaction=None, **kwargs):
        """Replace the current view with a new one (no stack history saved).

        Use this for one-way transitions where going "back" doesn't apply,
        such as welcome screen -> main dashboard.

        ``view_or_class`` accepts either a view class (constructed
        internally with ``**kwargs``) or a pre-constructed view instance
        (used directly; ``**kwargs`` must be empty). The instance form
        unblocks views built by async classmethods like
        ``PaginatedLayoutView.from_data`` and ``from_cursor``.

        The new view has no message yet: send it. This view closes as its
        ``exit()`` would, so its ``exit_policy`` freezes or deletes its
        message, a persistent panel's registration is removed, and views
        attached to it close. A swap that fails leaves this view working.

        Waits for a ``push()`` or ``pop()`` from this view that is still in
        flight. If it failed, this view is replaced as usual. If it landed,
        this view has handed its message on and ``replace()`` raises
        ``RuntimeError``, as ``push()`` and ``pop()`` do: replace from
        :attr:`current_view`. It raises too on the view a failed ``push()``
        or ``pop()`` returned, since the panel is still on the view the
        navigation started from. Waits the same way for a ``send()`` of this
        view still finishing. A view that has closed can still be replaced,
        which is how an ``on_timeout()`` override swaps the panel for a new
        message.
        """
        # Run beside a push in flight, its teardown and its failure path would
        # take the navigation's own state apart.
        await self._wait_to_navigate("replace()")
        # The screen on the message is another view's now: replacing from here
        # would close nothing and post a second live panel beside it.
        self._refuse_handed_over("replace()")
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

        # A cycle (A->B->C->A) would make every walk up the parent chain, this
        # one included, loop forever. The chain is collected for the message,
        # which names views by id since two instances of one class share a
        # type name.
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

        A view that navigated away with ``push()`` or ``pop()`` stands for
        the view that took its place, on either side: that is the one that
        exits later, and the one a cascade has to reach.

        Args:
            child_view: The child view to attach.
        """
        parent = self._last_successor()
        child_view = child_view._last_successor()
        parent._check_attachment(child_view)

        if child_view in parent._attached_children:
            return

        # Re-parent: detach from old parent if attached elsewhere
        old_parent = child_view._attached_to
        if old_parent is not None and old_parent is not parent:
            try:
                old_parent._attached_children.remove(child_view)
            except ValueError:
                pass

        parent._attached_children.append(child_view)
        child_view._attached_to = parent

    async def exit_children(self, *, delete_message: bool | None = True) -> None:
        """Exit every view attached to this one, and leave this view open.

        The same cascade this view's own ``exit()`` and timeout run, for a
        parent that stays up while its companions go: a game view closing
        its players' private panels when a round ends. A child with a push
        or pop in flight is waited out, and the view that took its place is
        the one closed. A view attached while this runs, or afterwards,
        belongs to a new set, closed by the next call or by this view's own
        exit.

        A view that handed its panel on (a ``push()`` or ``pop()`` from it,
        or an ephemeral panel's Continue button) handed its children to the
        view that took its place, so on it this closes nothing.

        Args:
            delete_message: Passed to the ``exit()`` of each child still
                open. ``True`` (the default, as in the cascade) deletes its
                message, ``False`` freezes it, and ``None`` leaves it to the
                child's ``exit_policy``. A child that already closed, by its
                own timeout or ``exit()``, keeps what that close left.

        Raises:
            RuntimeError: Called from inside a push or pop a child takes
                part in: from the new view's ``on_load()`` or a ``rebuild=``
                or ``nav_rebuild`` hook.
        """
        self._warn_if_handed_over("exit_children()")
        method = "A parent's exit_children()"
        self._refuse_cascade_into_own_navigation(method)
        await self._cleanup_attached_children(
            delete_message=delete_message, method=method, leave_new=True
        )

    async def _cleanup_attached_children(
        self,
        *,
        delete_message: bool | None = True,
        method: str = "A parent's exit()",
        leave_new: bool = False,
    ):
        """Exit all tracked child views that are still alive.

        Finished entries are dropped silently. Stale references can accumulate
        across long-lived parents (e.g. a game view whose ephemeral fleet
        panels were refreshed via ``auto_refresh_ephemeral``); pruning here
        keeps the list bounded without requiring callers to manually untrack.

        Each child stays in the list until its close returns, so another
        cascade running at the same time (the parent's own exit during an
        ``exit_children()``), the scan ``exit()`` makes first, and a later
        cleanup after a cancel all still find it. A view attached while the
        cascade runs is closed too, unless ``leave_new`` is set: that is
        ``exit_children()``, where this view stays open and a view attached
        after the call began belongs to the next set.

        The cascade is wrapped in ``store.batch()`` so N cascading
        ``VIEW_DESTROYED`` dispatches collapse into one ``BATCH_COMPLETE``
        notification. Subscribers see the final post-cleanup state once.

        No ``source_id`` is threaded: every child unsubscribes before its own
        ``VIEW_DESTROYED`` dispatch, so no subscriber in the fan-out owns the
        interaction's ack, and fire-and-forget is the right shape for this
        batch.
        """
        async with self.state_store.batch():
            # id -> view: holding the view keeps its id from being reused by
            # one attached later in the same cascade.
            seen = {}
            pending = [c for c in self._attached_children if c is not None]
            while True:
                if not pending:
                    if leave_new:
                        break
                    pending = [
                        c for c in self._attached_children if c is not None and id(c) not in seen
                    ]
                    if not pending:
                        break
                listed = pending.pop(0)
                if id(listed) in seen:
                    continue
                seen[id(listed)] = listed
                # A push or pop in flight hands the child's place to its
                # destination if the edit lands, reading the parent link, so the
                # link stays until the outcome is known.
                await listed._wait_out_navigation(method)
                child = listed
                while child._successor is not None and id(child._successor) not in seen:
                    child = child._successor
                    seen[id(child)] = child
                # Cleared before the close, not after: a child that kept the
                # back-pointer while tearing down would name a parent that is
                # dropping it.
                child._attached_to = None
                if child.is_finished():
                    # Torn down fully, not only unsubscribed: later cleanups skip
                    # a view that reads as torn down, so one left in the
                    # registries would stay there. Its own close runs it once,
                    # and a timeout still to run freezes it afterwards.
                    await child._close_stopped()
                else:
                    try:
                        await child._exit_or_successor(delete_message=delete_message)
                    except Exception as e:
                        # Dropped below either way, so a child that cannot
                        # tear itself down leaves nothing else to find it by.
                        logger.debug(
                            f"Attached child {type(child).__name__} failed to exit "
                            f"under {type(self).__name__}: {type(e).__name__}: {e}"
                        )
                for view in (listed, child):
                    if view in self._attached_children:
                        self._attached_children.remove(view)
