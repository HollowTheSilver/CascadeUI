# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import logging
import re
from typing import ClassVar, Dict

import discord

from ..state.actions import ActionCreators
from .base import _class_path
from .layout import StatefulLayoutView
from .view import StatefulView

logger = logging.getLogger(__name__)


# // ========================================( Registry )======================================== // #


# Class session key (module.QualName, or a session_class_key pin) -> class, for
# every persistent class. Rows record the same key in view_class, so a pinned
# class keeps reattaching after it moves.
_persistent_view_classes: Dict[str, type] = {}


# // ========================================( Mixin )======================================== // #


class _PersistentMixin:
    """Shared machinery for PersistentView and PersistentLayoutView.

    V1 and V2 persistent views share ~90% of their behavior: subclass
    auto-registration, timeout coercion, persistence_key validation, duplicate-
    key cleanup, exit dispatch, and the restore hook. The custom_id check
    is the divergence: V1 refuses a flat item with no id, V2 walks the tree and
    skips display items, which carry none.
    """

    # Persistent views are typically shared panels (role selectors, dashboards)
    owner_only: bool = False
    _persistent: bool = True

    # Bump in subclasses when ``__init__`` signature changes, and register
    # a matching ``register_kwargs_migrator`` to upgrade stored rows from
    # the previous version. Rows whose stored version is lower than this
    # with no registered migrator are logged and skipped on rehydrate.
    kwargs_schema_version: int = 1

    # A send under a key another panel holds retires that panel; False leaves
    # it to the caller, for a swap confirmed before the old one comes down.
    retire_previous_on_send: bool = True
    _BOOL_ATTRS: ClassVar[tuple] = ("retire_previous_on_send",)
    # Describes the constructor's shape, which is the class's.
    _CLASS_ONLY_ATTRS: ClassVar[tuple] = ("kwargs_schema_version",)

    # discord.py auto-generates custom_ids as 32-char hex strings (os.urandom(16).hex())
    _AUTO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

    @classmethod
    def _validate_attribute_value(cls, name: str, value) -> None:
        """Extend the base dispatch with the ``kwargs_schema_version`` check.

        The version is read with ``int()`` at every registry write, so a
        value that is not a positive int, ``None`` included, would fail the
        panel's first write instead of its class definition, and the panel
        would not be restored after the next restart.
        """
        if name == "kwargs_schema_version":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{cls.__name__}.{name} must be a positive int, got {value!r}")
            return
        super()._validate_attribute_value(name, value)

    @classmethod
    def _validate_class_attributes(cls) -> None:
        super()._validate_class_attributes()
        if "kwargs_schema_version" in cls.__dict__:
            cls._validate_attribute_value(
                "kwargs_schema_version", cls.__dict__["kwargs_schema_version"]
            )

    def __init_subclass__(cls, **kwargs):
        """Auto-register every concrete subclass so restore can find it by name."""
        # Checked before super() runs, because super() registers the class in
        # the view registry: a rejection that has already mutated a registry
        # leaves the refused class resolvable by a later pop.
        cls._validate_session_class_key()
        key = cls._class_session_key()
        existing = _persistent_view_classes.get(key)
        # Two classes sharing a key would reattach every row as whichever was
        # defined last. A cog reload re-registers the same class path, which is
        # the overwrite meant, so the paths tell the two cases apart.
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
        # The registration this panel superseded without retiring it, for its
        # exit() to hand back (see _reregister_live_predecessor).
        self._superseded_registration = None

        if self._persistence_key is None:
            raise ValueError(
                f"{self.__class__.__name__} requires a 'persistence_key' argument. "
                "Persistent views need a stable key to track their message across restarts."
            )

    def _iter_persistent_items(self):
        """Every item in the tree to check for an explicit custom_id.

        Nested items included, and here rather than on
        ``PersistentLayoutView``: the persistent leaderboard and roles panels
        compose this mixin with a V2 pattern and never pass through it, so a
        walk defined there would leave them checking only their top-level
        containers.
        """
        return self.walk_children()

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

    def _validate_loaded_tree(self) -> None:
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
        """Close a message left by an earlier run's panel under this persistence_key.

        ``exit_policy`` decides, as it does when a live predecessor is exited:
        the message is deleted, or frozen as it stands (a V1 message loses its
        buttons and keeps its embed). Whether the earlier panel was restored
        in this process does not change what happens to its message.
        """
        await self._close_other_message(old_message, old_message.components)

    async def _register_persistent(self, message) -> None:
        """Perform duplicate-key cleanup and dispatch PERSISTENT_VIEW_REGISTERED.

        Called from :meth:`_after_send` once the send has posted the
        message, and to hand a registration back to a live predecessor.
        """
        registry = self.state_store.state.get("persistent_views", {})
        existing = registry.get(self.persistence_key)
        # This panel sent again: the send closed the message it left, as its
        # exit_policy says, so that message is neither a predecessor to retire
        # nor an orphan to clean up.
        moved = existing is not None and existing.get("message_id") == self._registry_message_id
        # Moving its own row keeps the record of the panel it superseded.
        if not moved:
            self._superseded_registration = (
                dict(existing)
                if not self.retire_previous_on_send
                and existing
                and existing.get("message_id") != str(message.id)
                else None
            )

        # Record this view in the persistent registry
        guild_id = None
        if message.guild:
            guild_id = str(message.guild.id)

        payload = ActionCreators.persistent_view_registered(
            persistence_key=self.persistence_key,
            class_name=type(self)._class_session_key(),
            message_id=str(message.id),
            channel_id=self._channel_id_of(message),
            guild_id=guild_id,
            # is not None, not truthiness: id 0 is a value, and storing it as
            # None would drop owner_only on the restored view.
            user_id=str(self.user_id) if self.user_id is not None else None,
        )
        previous = self._registry_message_id
        self._registry_message_id = str(message.id)
        try:
            await self.dispatch("PERSISTENT_VIEW_REGISTERED", payload)
        except BaseException:
            # Not registered: the view keeps the entry it had, which its exit()
            # removes. Only while the id is still the one written above: a bot's
            # close clears it, and restoring it then would let a stale exit()
            # remove the row a restart restores from.
            entry = self.state_store.state.get("persistent_views", {}).get(self.persistence_key)
            if self._registry_message_id == str(message.id) and (
                entry is None or entry.get("message_id") != str(message.id)
            ):
                self._registry_message_id = previous
            raise

        # Retired after this view owns the key, so each predecessor's exit()
        # unregisters a message the entry no longer points at and removes
        # nothing. Retiring first let that exit clear the key and hand it to
        # the next live panel under it, which is this one, mid-registration.
        if (
            self.retire_previous_on_send
            and existing
            and not moved
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
            if superseded_message is not None:
                holders += [
                    view
                    for view in self.state_store._views_for_message(superseded_message)
                    if view not in holders
                ]
            for old_view in holders:
                if old_view is not self:
                    old_view_exited = True
                    # One holder's exit can retire another (a paired panel), and
                    # a holder can hand its place on; counted as retired either
                    # way, so the orphan-message fallback below is not owed.
                    if await old_view._exit_or_successor():
                        logger.info(
                            f"Exited previous view instance for persistence_key "
                            f"'{self.persistence_key}'"
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
        self._refuse_send()
        self._validate_custom_ids()
        return await super().send(*args, ephemeral=ephemeral, **kwargs)

    async def _prepare_send(self) -> None:
        """Bind runtime dependencies before ``on_load`` and the first render need them.

        Inside the send, so a panel closed while ``on_bind`` runs is not
        posted. No-op when the context resolves no bot.
        """
        await super()._prepare_send()
        await self._bind_from_context()

    async def _after_send(self, message) -> None:
        """Register the panel once its message is posted.

        Inside the send, so a close requested while the panel was being sent
        is carried out after the registration exists, and a panel closed
        before it posted never registers.
        """
        await super()._after_send(message)
        # Same contract as the send pipeline's own post-send tail: the
        # message exists, so a failure here is reported rather than raised.
        # The consequence is named because it is invisible until the next
        # restart.
        try:
            await self._register_persistent(message)
        except BaseException as e:
            key = f"persistence_key {self.persistence_key!r}"
            if not isinstance(e, Exception):
                # A cancel: raised at once, since anything awaited here would
                # run before the send's bound on a cancel starts. The row left
                # naming the earlier message goes when the send closes it.
                logger.warning(
                    f"{type(self).__name__} was sent, but {type(e).__name__} cut off "
                    f"registering {key}. The message is live and interactive, and "
                    f"{self._registration_after_failure(message)}."
                )
                raise
            outcome = self._registration_after_failure(message)
            logger.error(
                f"{type(self).__name__} was sent, but registering {key} raised "
                f"{type(e).__name__}: {e}. The message is live and interactive, "
                f"and {outcome}.",
                exc_info=e,
            )

    async def _retire_registration_left_behind(self, posted) -> None:
        """Remove the row still naming the message this send left, not ``posted``.

        The registration did not move to the new message: it raised, a cancel
        cut it off, or the panel was stopped or closed during the send. The
        send closes the message it left, so a restart would restore the panel
        onto a message with nothing to click, and code checking whether the
        panel is up would find it there. A bot's close clears the view's
        registration id, which keeps the row for the restart to restore from.
        Under ``retire_previous_on_send = False`` an earlier panel still live
        under the key takes the registration back, as when this one exits.
        ``posted`` is ``None`` when a render found the new message deleted.
        """
        key = self.persistence_key
        held = self._registry_message_id
        entry = self.state_store.state.get("persistent_views", {}).get(key)
        if (
            held is None
            or (posted is not None and held == str(posted.id))
            or entry is None
            or entry.get("message_id") != held
        ):
            return
        try:
            await self.dispatch(
                "PERSISTENT_VIEW_UNREGISTERED",
                ActionCreators.persistent_view_unregistered(key, message_id=held),
            )
        except Exception as e:
            logger.debug(f"{type(self).__name__} could not remove its earlier registration: {e}")
            return
        self._registry_message_id = None
        if not self.retire_previous_on_send and key not in self.state_store.state.get(
            "persistent_views", {}
        ):
            await self._hand_back(key)

    def _registration_after_failure(self, message) -> str:
        """Describe where a failed registration left the panel's row."""
        entry = self.state_store.state.get("persistent_views", {}).get(self.persistence_key)
        named = None if entry is None else entry.get("message_id")
        if named == str(message.id):
            return "it is registered, but an earlier panel under that key may still be live"
        if named is None or named == self._registry_message_id:
            # A row naming the message this send leaves goes as it closes.
            return "it will not be reattached after a restart"
        return f"its registration still names message {named}, not this one"

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
        # The registration is removed by _retire_registration, which exit()
        # calls once the attached children have closed: an exit cut off before
        # then leaves the panel live and still registered.
        return await super().exit(delete_message=delete_message)

    async def _retire_registration(self) -> None:
        """Remove this panel's registration as its ``exit()`` commits."""
        key = self.persistence_key
        entry = self.state_store.state.get("persistent_views", {}).get(key)
        owned = entry is not None
        # A view that never registered owns nothing, and a payload with no
        # message id removes the key whatever holds it, so exiting a panel
        # whose send raised, or was refused by the instance limit, would
        # retire the registration of the panel actually on screen.
        registered_elsewhere = (
            self._registry_message_id is None and owned and entry.get("message_id") is not None
        )
        if owned and not registered_elsewhere:
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
            await self._hand_back(key)
        # A registration carried from the panel this one was pushed from.
        await super()._retire_registration()

    async def on_bind(self, bot):
        """Inject non-serializable runtime dependencies from ``bot``.

        A persistent view often needs runtime handles (a database pool, the
        bot, a service client) that cannot ride the constructor through the
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

        Runs first thing inside :meth:`send`, before ``on_pre_send`` and the
        render pipeline, so the view's dependencies are in place for
        ``on_load`` and the first render.
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

        Runs after ``bot.wait_until_ready()`` (on a background task, off the
        ``setup_hook`` critical path), so the gateway cache (``get_user``,
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
