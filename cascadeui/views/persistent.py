# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import logging
import re
from typing import ClassVar, Dict, Optional

import discord

from ..state.actions import ActionCreators
from .base import _class_path
from .layout import StatefulLayoutView
from .view import StatefulView

logger = logging.getLogger(__name__)


# // ========================================( Registry )======================================== // #


# Maps the class session key (module.QualName, or a ``session_class_key`` pin
# when set) -> class, for all PersistentView subclasses. Rows record the same
# key in their ``view_class`` column, which is what lets a pinned class keep
# reattaching after it moves. The qualified default prevents cross-module
# collisions when two unrelated cogs define a class with the same bare name
# (e.g. ``TicketPanel`` in two different bots).
_persistent_view_classes: Dict[str, type] = {}


# // ========================================( Mixin )======================================== // #


class _PersistentMixin:
    """Shared machinery for PersistentView and PersistentLayoutView.

    V1 and V2 persistent views share ~90% of their behavior: subclass
    auto-registration, timeout coercion, persistence_key validation, duplicate-
    key cleanup, exit dispatch, and the restore hook. The only genuine
    divergences are captured as hook methods:

    - ``_iter_persistent_items``: V1 walks ``self.children``, V2 walks
      ``self.walk_children()``.
    - ``_cleanup_orphan_message``: V1 calls ``edit(view=None)``, V2 calls
      ``delete()`` because V2 messages ARE their components.
    """

    # Persistent views are typically shared panels (role selectors, dashboards)
    owner_only: bool = False
    _persistent: bool = True

    # Bump in subclasses when ``__init__`` signature changes, and register
    # a matching ``register_kwargs_migrator`` to upgrade stored rows from
    # the previous version. Rows whose stored version is lower than this
    # with no registered migrator are logged and skipped on rehydrate.
    kwargs_schema_version: int = 1

    # A send under a persistence_key another panel still holds retires that
    # panel: a live instance is exited, a message left from before a restart is
    # cleaned up. False leaves the predecessor to the caller, for a swap that
    # confirms the new panel before the old one comes down.
    retire_previous_on_send: bool = True
    _BOOL_ATTRS: ClassVar[tuple] = ("retire_previous_on_send",)

    # The message id this instance registered or was restored under. exit()
    # sends it with its unregister so a superseded panel cannot remove the row
    # its successor now owns; _message is already None by the time a deleted
    # message's cleanup calls exit(), so it cannot be read from there.
    _registry_message_id: Optional[str] = None

    # discord.py auto-generates custom_ids as 32-char hex strings (os.urandom(16).hex())
    _AUTO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

    def __init_subclass__(cls, **kwargs):
        """Auto-register every concrete subclass so restore can find it by name."""
        # Checked before super() runs, because super() registers the class in
        # the view registry: a rejection that has already mutated a registry
        # leaves the refused class resolvable by a later pop.
        cls._validate_session_class_key()
        key = cls._class_session_key()
        existing = _persistent_view_classes.get(key)
        # One slot per key, so two classes sharing a session_class_key means
        # the second silently displaces the first and every row written by
        # either reattaches as whichever was defined last. Refused here
        # because there is no correct outcome to pick at restore time. A cog
        # reload re-registers the same class path and is the intended
        # overwrite, so the paths are what distinguish the two cases.
        if existing is not None and _class_path(existing) != _class_path(cls):
            raise ValueError(
                f"{_class_path(cls)} and {_class_path(existing)} both resolve to the "
                f"persistent class key {key!r}, so stored rows cannot say which class "
                f"wrote them and would all reattach as one."
                f"\n  Fix: give each class its own session_class_key. If one of them is "
                f"the old definition of a class you moved, delete that one rather than "
                f"changing the pin -- the pin has to keep matching what the rows hold."
            )
        super().__init_subclass__(**kwargs)
        _persistent_view_classes[key] = cls

    def __init__(self, *args, **kwargs):
        # Persistent views never time out
        kwargs["timeout"] = None

        super().__init__(*args, **kwargs)

        if self._persistence_key is None:
            raise ValueError(
                f"{self.__class__.__name__} requires a 'persistence_key' argument. "
                "Persistent views need a stable key to track their message across restarts."
            )

    def _iter_persistent_items(self):
        """Iterable of items to validate for custom_id presence.

        Subclasses override to use the appropriate traversal for their
        component model. V1 uses flat ``self.children``; V2 uses
        ``self.walk_children()`` to descend into Containers and ActionRows.
        """
        return self.children

    def validate(self) -> None:
        """Raise if this view's tree is one Discord or a restart would reject.

        Extends the base check with the id validation a persistent
        ``send()`` runs. Without it the public entry would pass a panel
        whose auto-generated ids do not survive a restart, which is the
        failure this class exists to prevent and the one an offline test
        most needs to reach.
        """
        super().validate()
        self._validate_custom_ids()

    def _validate_custom_ids(self):
        """Ensure every interactive component has an explicit custom_id.

        Non-interactive items (Container, TextDisplay, Separator) have
        ``custom_id=None`` and are intentionally skipped -- only items
        with a discord.py auto-generated ID (32-char hex) indicate a
        missing explicit custom_id.
        """
        for item in self._iter_persistent_items():
            custom_id = getattr(item, "custom_id", None)
            if custom_id is None:
                # V1 treats None as an error; V2 treats None as non-interactive.
                # The V1 override handles the stricter check before calling super.
                continue
            if getattr(item, "_cascadeui_stabilized", False) or self._AUTO_ID_PATTERN.match(
                custom_id
            ):
                raise ValueError(
                    f"Component {item!r} in {self.__class__.__name__} is missing a custom_id. "
                    "All interactive components in a persistent view must have an explicit "
                    "custom_id so discord.py can re-attach them after a restart."
                )

    async def _cleanup_orphan_message(self, old_message):
        """Clean up a stale message left by a previous instance of this persistence_key.

        V1 calls ``edit(view=None)`` (strips buttons, keeps embed/content).
        V2 calls ``delete()`` because V2 messages ARE their components  --
        ``edit(view=None)`` would produce an empty message (Discord error 50006).
        """
        await self._bounded(old_message.edit(view=None))

    async def _register_persistent(self, message) -> None:
        """Perform duplicate-key cleanup and dispatch PERSISTENT_VIEW_REGISTERED.

        Called by subclass ``send()`` implementations after the message
        has been successfully sent through the View/LayoutView pipeline.
        """
        registry = self.state_store.state.get("persistent_views", {})
        existing = registry.get(self.persistence_key)

        # Record this view in the persistent registry
        guild_id = None
        if message.guild:
            guild_id = str(message.guild.id)

        payload = ActionCreators.persistent_view_registered(
            persistence_key=self.persistence_key,
            class_name=type(self)._class_session_key(),
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            guild_id=guild_id,
            # is not None, not truthiness: id 0 is a value, and storing it as
            # None would drop owner_only on the restored view.
            user_id=str(self.user_id) if self.user_id is not None else None,
        )
        self._registry_message_id = str(message.id)
        await self.dispatch("PERSISTENT_VIEW_REGISTERED", payload)

        # Retired after this view owns the key, so each predecessor's exit()
        # unregisters a message the entry no longer points at and removes
        # nothing. Retiring first let that exit clear the key and hand it to
        # the next live panel under it, which is this one, mid-registration.
        if (
            self.retire_previous_on_send
            and existing
            and existing.get("message_id") != str(message.id)
        ):
            # Exit every older instance still alive in this process. exit()
            # handles the full cleanup: unsubscribe, disable components on the
            # message, and dispatch VIEW_DESTROYED.
            old_view_exited = False
            # The key's holders, plus any view showing the superseded panel's
            # message without its key: a child the panel pushed carries the
            # registration id, and would otherwise stay live on that message.
            holders = self.state_store._views_for_key(self.persistence_key)
            superseded_message = existing.get("message_id")
            holders += [
                view
                for view in self.state_store.get_active_views().values()
                if view not in holders
                and superseded_message is not None
                and getattr(view, "_registry_message_id", None) == superseded_message
            ]
            for old_view in holders:
                if old_view is not self:
                    old_view_exited = True
                    # Retiring one holder can retire another (a paired panel
                    # exits its partner), and the list predates the first
                    # exit. Counted as retired either way: it is gone, so the
                    # orphan-message fallback below is not owed.
                    if old_view._torn_down():
                        continue
                    await old_view.exit()
                    logger.info(
                        f"Exited previous view instance for persistence_key '{self.persistence_key}'"
                    )

            # If the old view wasn't alive (e.g. from a previous bot session that
            # wasn't restored), fall back to message-only cleanup.
            if not old_view_exited:
                old_msg_id = existing["message_id"]
                old_ch_id = existing["channel_id"]
                bot = getattr(self.context, "bot", None) or getattr(
                    self.interaction, "client", None
                )
                if bot:
                    try:
                        old_channel = bot.get_channel(int(old_ch_id))
                        if old_channel and isinstance(old_channel, discord.abc.Messageable):
                            old_message = await old_channel.fetch_message(int(old_msg_id))
                            await self._cleanup_orphan_message(old_message)
                            logger.info(
                                f"Cleaned up previous message {old_msg_id} for "
                                f"persistence_key '{self.persistence_key}'"
                            )
                    except (
                        discord.NotFound,
                        discord.Forbidden,
                        discord.HTTPException,
                        asyncio.TimeoutError,
                    ):
                        pass  # Old message already gone, nothing to clean up
                    except Exception as e:
                        logger.debug(
                            f"Could not clean up previous message for '{self.persistence_key}': {e}"
                        )

    async def send(self, *args, ephemeral: bool = False, **kwargs):
        """Send the view and register it for persistence.

        Single seam shared by every persistent view -- including
        composed patterns like ``PersistentRolesLayoutView`` and
        ``PersistentLeaderboardLayoutView`` whose MRO does not pass
        through ``PersistentView`` or ``PersistentLayoutView``.
        ``*args`` / ``**kwargs`` propagate intact to whichever concrete
        ``send()`` sits below this mixin in MRO (V1 takes
        ``content``/``embed``/``embeds``; V2 takes only ``ephemeral``).
        """
        if ephemeral:
            raise ValueError(
                f"{self.__class__.__name__} cannot be sent as ephemeral. "
                "Persistent views require a real channel message to survive bot restarts. "
                "Ephemeral messages have no permanent ID and cannot be re-attached."
            )
        self._validate_custom_ids()

        # Bind runtime deps before the render pipeline so on_load and the
        # first render have them. No-op when the context resolves no bot.
        await self._bind_from_context()

        message = await super().send(*args, ephemeral=ephemeral, **kwargs)

        if message is None:
            return message

        # Same contract as the send pipeline's own post-send tail: the
        # message exists, so a failure here is reported rather than raised.
        # The consequence is named because it is invisible until the next
        # restart, when the panel does not come back.
        try:
            await self._register_persistent(message)
        except Exception as e:
            logger.error(
                f"{type(self).__name__} was sent, but registering persistence_key "
                f"{self.persistence_key!r} raised {type(e).__name__}: {e}. The "
                f"message is live and interactive, and will not be reattached "
                f"after a restart.",
                exc_info=e,
            )
        return message

    async def _reregister_live_predecessor(self, key: str) -> None:
        """Register the most recent other live panel under ``key``, if any.

        Called only for a panel with ``retire_previous_on_send = False``, the
        setting under which an earlier panel stays live beside it. A finished
        panel is skipped: a caller that stopped it has already retired it.
        """
        for view in self.state_store._views_for_key(key):
            if view is not self and view._message is not None and not view.is_finished():
                await view._register_persistent(view._message)
                return

    async def exit(self, delete_message: bool | None = None):
        """Exit the view and remove it from the persistent registry.

        Retiring a stored registration when no instance is live goes through
        ``PersistenceManager.prune_registry(persistence_keys=[...])`` instead,
        reachable as ``store.persistence_manager``. The two are not
        interchangeable: this route retires the registration only while the
        view still owns it, while ``prune_registry`` matches by key and takes
        whatever holds it. Retiring a panel that may have a live successor or
        predecessor belongs here; a prune belongs after the exit, never
        before, and only for a key no live panel holds.

        A panel superseded under its key (see ``retire_previous_on_send``)
        no longer owns the stored registration, so its ``exit()`` leaves the
        successor's row in place. When the panel that owns the registration
        exits while an earlier panel is still live under the same key, the
        registration moves back to that panel, so a swap rolled back by
        exiting the new panel leaves the old one restorable.
        """
        key = self.persistence_key
        entry = self.state_store.state.get("persistent_views", {}).get(key)
        owned = entry is not None
        # A view that never registered owns nothing, and a payload with no
        # message id removes the key whatever holds it -- so exiting a panel
        # whose send raised, or was refused by the instance limit, would
        # retire the registration of the panel actually on screen.
        registered_elsewhere = (
            self._registry_message_id is None and owned and entry.get("message_id") is not None
        )
        if not registered_elsewhere:
            # Unregister before cleanup so the state dispatch still works
            payload = ActionCreators.persistent_view_unregistered(
                key, message_id=self._registry_message_id
            )
            await self.dispatch("PERSISTENT_VIEW_UNREGISTERED", payload)
        # Only a panel that opted out of retiring its predecessor can have one
        # still live. Under the default, another holder of the key is a panel
        # mid-send, which registers itself once its send completes.
        if (
            not self.retire_previous_on_send
            and owned
            and key not in self.state_store.state.get("persistent_views", {})
        ):
            try:
                await self._reregister_live_predecessor(key)
            except Exception as e:
                # Reported rather than raised, like the registration in send():
                # the teardown below still has to run, and a caller rolling a
                # swap back would otherwise be left with both panels live.
                logger.error(
                    f"Handing {key!r} back to the live predecessor raised "
                    f"{type(e).__name__}: {e}. The registration is removed and no "
                    f"panel holds it, so none is restored after a restart; re-send "
                    f"the panel to register it again.",
                    exc_info=e,
                )

        # super().exit() -> stop() -> _redrive_dynamic_items() repairs
        # discord.py's shared dynamic-item registry; the base implementation
        # covers every view, persistent or not, so there is nothing
        # persistent-specific left to do here.
        return await super().exit(delete_message=delete_message)

    async def on_bind(self, bot):
        """Inject non-serializable runtime dependencies from ``bot``.

        A persistent view often needs runtime handles -- a database pool, the
        bot, a service client -- that cannot ride the constructor through the
        persistence round-trip, because the registry row is JSON and these
        objects are not serializable. ``on_bind`` is the seam for supplying
        them from ``bot``, the one handle available both when the view is sent
        and when it is restored. Default is a no-op::

            async def on_bind(self, bot):
                self.db = bot.db
                self.bot = bot

        The library calls ``on_bind(bot)`` automatically at two points: during
        ``send()`` (when ``bot`` is derivable from the construction context)
        and during restore on restart, before :meth:`on_restore`. A view
        posted with a bare channel context carries no ``.bot``, so ``send()``
        cannot derive it -- such a view calls ``await view.on_bind(bot)``
        itself before ``send()``. The hook stays idempotent so the occasional
        double call is harmless. A sync override is also accepted.

        ``on_bind`` is for attribute assignment, not UI side effects: the
        view is not displayed yet when it runs (the first render and
        :meth:`on_restore` both run after it), so a ``refresh()`` or
        ``send()`` call from the hook is premature. Load data in
        :meth:`on_load` or :meth:`on_restore`.

        Args:
            bot: The discord.py Bot instance.
        """
        return None

    async def _bind_from_context(self):
        """Call ``on_bind`` with the bot when the context can resolve it.

        Runs at the top of :meth:`send`, before the render pipeline, so the
        view's dependencies are in place for ``on_load`` and the first render.
        A channel-context send resolves no bot and is left to the caller's own
        ``on_bind`` call.
        """
        bot = getattr(self.interaction, "client", None) or getattr(self.context, "bot", None)
        if bot is None:
            return
        result = self.on_bind(bot)
        if inspect.isawaitable(result):
            await result

    async def on_restore(self, bot):
        """Called after the view is reconstructed on bot restart.

        Override this to perform post-restore setup like fetching fresh data
        or updating the embed. The view's ``_message`` is already set when
        this is called, and any attributes ``on_bind`` injects are set.

        Runs after ``bot.wait_until_ready()`` -- on a background task, off the
        ``setup_hook`` critical path -- so the gateway cache (``get_user``,
        guild members, channels) is warm. A render that reads those caches
        resolves real values instead of cold defaults. Interaction
        routing is registered earlier, during reattach, so the view is
        clickable before this render runs.

        .. warning::
            If this method raises an exception, the view will be unregistered
            from CascadeUI's state system but will remain in discord.py's
            internal view store (there is no public API to remove it). Avoid
            raising from this method unless recovery is not possible.

        .. note::
            ``attachment://`` references already present in the persisted
            component tree resolve from Discord's stored copy of the
            original upload -- the bytes survive bot restarts as long as
            the message itself does. New ``attachment://`` references
            introduced inside this hook (or any subsequent rebuild) need a
            matching ``discord.File`` passed to
            ``view.refresh(attachments=[...])`` so the bytes travel with
            the edit; otherwise Discord reports the reference as
            unresolved.

        Args:
            bot: The discord.py Bot instance.
        """
        pass


# // ========================================( Classes )======================================== // #


class PersistentView(_PersistentMixin, StatefulView):
    """A view that survives bot restarts by re-attaching to its original message.

    Subclass this instead of StatefulView for long-lived UI like role selectors,
    ticket panels, or dashboards that should stay interactive indefinitely.

    Requirements:
        - Must provide a ``persistence_key`` at init (used to track the view in state).
        - All components must have an explicit ``custom_id`` (discord.py requirement
          for persistent views).
        - ``timeout`` is forced to ``None`` so the view never expires.

    After sending, the view's message location is saved to state. On bot
    restart, :class:`~cascadeui.state.middleware.PersistenceMiddleware`
    constructed with ``bot=self`` re-attaches every registered view during
    its ``initialize`` pass when passed to
    :func:`~cascadeui.setup_middleware` from ``setup_hook``.
    """

    def _validate_custom_ids(self):
        """V1 override: every interactive child must have an explicit custom_id.

        V1 views are flat, so a ``None`` custom_id normally indicates a
        missing id rather than a non-interactive container. Link and premium
        buttons are the exception: they carry no custom_id and need none, so
        they are skipped.
        """
        for item in self.children:
            # Link and premium buttons have no custom_id and need none: the
            # platform handles them, so there is nothing for discord.py to
            # re-attach after a restart.
            if getattr(item, "url", None) is not None or getattr(item, "sku_id", None) is not None:
                continue
            custom_id = getattr(item, "custom_id", None)
            if (
                custom_id is None
                or getattr(item, "_cascadeui_stabilized", False)
                or self._AUTO_ID_PATTERN.match(custom_id)
            ):
                raise ValueError(
                    f"Component {item!r} in {self.__class__.__name__} is missing a custom_id. "
                    "All components in a PersistentView must have an explicit custom_id "
                    "so discord.py can re-attach them after a restart."
                )


class PersistentLayoutView(_PersistentMixin, StatefulLayoutView):
    """A V2 layout view that survives bot restarts.

    The V2 equivalent of ``PersistentView``. Subclass this instead of
    ``StatefulLayoutView`` for long-lived V2 UI that should stay interactive
    indefinitely.

    Requirements are the same as ``PersistentView``: ``persistence_key`` at init,
    explicit ``custom_id`` on all interactive components, and no ephemeral sends.
    """

    def _iter_persistent_items(self):
        """V2 tree traversal: descend into Containers and ActionRows."""
        return self.walk_children()

    async def _cleanup_orphan_message(self, old_message):
        """V2 messages ARE their components -- delete instead of edit."""
        await self._bounded(old_message.delete())
