# // ========================================( Modules )======================================== // #


import asyncio
import inspect
import logging
import re
from typing import Dict

import discord

from ..state.actions import ActionCreators
from .layout import StatefulLayoutView
from .view import StatefulView

logger = logging.getLogger(__name__)


# // ========================================( Registry )======================================== // #


# Maps fully-qualified class path (module.QualName) -> class for all PersistentView subclasses.
# The qualified key prevents cross-module collisions when two unrelated cogs define a class
# with the same bare name (e.g. ``TicketPanel`` in two different bots).
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

    # discord.py auto-generates custom_ids as 32-char hex strings (os.urandom(16).hex())
    _AUTO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

    def __init_subclass__(cls, **kwargs):
        """Auto-register every concrete subclass so restore can find it by name."""
        super().__init_subclass__(**kwargs)
        _persistent_view_classes[cls._class_session_key()] = cls

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
            if self._AUTO_ID_PATTERN.match(custom_id):
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
        # If this persistence_key already has a registered view/message, clean up the old
        # one to prevent orphaned views and messages.
        registry = self.state_store.state.get("persistent_views", {})
        existing = registry.get(self.persistence_key)
        if existing and existing.get("message_id") != str(message.id):
            # Try to exit the old view instance if it's still alive in this process.
            # exit() handles the full cleanup: unsubscribe, unregister, disable
            # components on the message, and dispatch VIEW_DESTROYED.
            old_view_exited = False
            for vid, old_view in list(self.state_store._active_views.items()):
                if (
                    getattr(old_view, "_persistence_key", None) == self.persistence_key
                    and old_view.id != self.id
                ):
                    await old_view.exit()
                    old_view_exited = True
                    logger.info(
                        f"Exited previous view instance for persistence_key '{self.persistence_key}'"
                    )
                    break

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
            user_id=str(self.user_id) if self.user_id else None,
        )
        await self.dispatch("PERSISTENT_VIEW_REGISTERED", payload)

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

        await self._register_persistent(message)
        return message

    async def exit(self, delete_message: bool | None = None):
        """Exit the view and remove it from the persistent registry."""
        # Unregister before cleanup so the state dispatch still works
        payload = ActionCreators.persistent_view_unregistered(self.persistence_key)
        await self.dispatch("PERSISTENT_VIEW_UNREGISTERED", payload)

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
            if custom_id is None or self._AUTO_ID_PATTERN.match(custom_id):
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
