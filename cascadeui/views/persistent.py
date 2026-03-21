
# // ========================================( Modules )======================================== // #


import re
from typing import Dict, Optional, Any, List

import discord

from .base import StatefulView
from ..state.singleton import get_store
from ..state.actions import ActionCreators
from ..utils.logging import AsyncLogger

logger = AsyncLogger(name=__name__, level="DEBUG", path="logs", mode="a", prefix="cascadeui")


# // ========================================( Registry )======================================== // #


# Maps class name -> class for all PersistentView subclasses
_persistent_view_classes: Dict[str, type] = {}


# // ========================================( Classes )======================================== // #


class PersistentView(StatefulView):
    """A view that survives bot restarts by re-attaching to its original message.

    Subclass this instead of StatefulView for long-lived UI like role selectors,
    ticket panels, or dashboards that should stay interactive indefinitely.

    Requirements:
        - Must provide a ``state_key`` at init (used to track the view in state).
        - All components must have an explicit ``custom_id`` (discord.py requirement
          for persistent views).
        - ``timeout`` is forced to ``None`` so the view never expires.

    After sending, the view's message location is saved to state. On bot restart,
    call ``setup_persistence(bot)`` in ``setup_hook`` to re-attach all
    registered views automatically.
    """

    # Persistent views are typically shared panels (role selectors, dashboards)
    owner_only: bool = False

    def __init_subclass__(cls, **kwargs):
        """Auto-register every concrete subclass so restore can find it by name."""
        super().__init_subclass__(**kwargs)
        _persistent_view_classes[cls.__name__] = cls

    def __init__(self, *args, **kwargs):
        # Persistent views never time out
        kwargs["timeout"] = None

        super().__init__(*args, **kwargs)

        if self._state_key is None:
            raise ValueError(
                f"{self.__class__.__name__} requires a 'state_key' argument. "
                "Persistent views need a stable key to track their message across restarts."
            )

    # discord.py auto-generates custom_ids as 32-char hex strings (os.urandom(16).hex())
    _AUTO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

    def _validate_custom_ids(self):
        """Ensure every component has an explicit (non-auto-generated) custom_id."""
        for item in self.children:
            custom_id = getattr(item, "custom_id", None)
            if custom_id is None or self._AUTO_ID_PATTERN.match(custom_id):
                raise ValueError(
                    f"Component {item!r} in {self.__class__.__name__} is missing a custom_id. "
                    "All components in a PersistentView must have an explicit custom_id "
                    "so discord.py can re-attach them after a restart."
                )

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False):
        """Send the view and register it for persistence."""
        if ephemeral:
            raise ValueError(
                f"{self.__class__.__name__} cannot be sent as ephemeral. "
                "Persistent views require a real channel message to survive bot restarts. "
                "Ephemeral messages have no permanent ID and cannot be re-attached."
            )
        self._validate_custom_ids()

        message = await super().send(content, embed=embed, embeds=embeds, ephemeral=ephemeral)

        if message is None:
            return message

        # If this state_key already has a registered view/message, clean up the old
        # one to prevent orphaned views and messages.
        registry = self.state_store.state.get("persistent_views", {})
        existing = registry.get(self.state_key)
        if existing and existing.get("message_id") != str(message.id):
            # Try to exit the old view instance if it's still alive in this process.
            # exit() handles the full cleanup: unsubscribe, unregister, disable
            # components on the message, and dispatch VIEW_DESTROYED.
            old_view_exited = False
            for vid, old_view in list(self.state_store._active_views.items()):
                if (getattr(old_view, "_state_key", None) == self.state_key
                        and old_view.id != self.id):
                    await old_view.exit()
                    old_view_exited = True
                    logger.info(
                        f"Exited previous view instance for state_key '{self.state_key}'"
                    )
                    break

            # If the old view wasn't alive (e.g. from a previous bot session that
            # wasn't restored), fall back to message-only cleanup.
            if not old_view_exited:
                old_msg_id = existing["message_id"]
                old_ch_id = existing["channel_id"]
                bot = (
                    getattr(self.context, "bot", None)
                    or getattr(self.interaction, "client", None)
                )
                if bot:
                    try:
                        old_channel = bot.get_channel(int(old_ch_id))
                        if old_channel and isinstance(old_channel, discord.abc.Messageable):
                            old_message = await old_channel.fetch_message(int(old_msg_id))
                            await old_message.edit(view=None)
                            logger.info(
                                f"Cleaned up previous message {old_msg_id} for "
                                f"state_key '{self.state_key}'"
                            )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass  # Old message already gone, nothing to clean up
                    except Exception as e:
                        logger.debug(
                            f"Could not clean up previous message for "
                            f"'{self.state_key}': {e}"
                        )

        # Record this view in the persistent registry
        guild_id = None
        if message.guild:
            guild_id = str(message.guild.id)

        payload = ActionCreators.persistent_view_registered(
            state_key=self.state_key,
            class_name=self.__class__.__name__,
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            guild_id=guild_id,
            user_id=str(self.user_id) if self.user_id else None,
        )
        await self.dispatch("PERSISTENT_VIEW_REGISTERED", payload)

        return message

    async def exit(self, delete_message=False):
        """Exit the view and remove it from the persistent registry."""
        # Unregister before cleanup so the state dispatch still works
        payload = ActionCreators.persistent_view_unregistered(self.state_key)
        await self.dispatch("PERSISTENT_VIEW_UNREGISTERED", payload)

        return await super().exit(delete_message=delete_message)

    async def on_restore(self, bot):
        """Called after the view is reconstructed on bot restart.

        Override this to perform post-restore setup like fetching fresh data
        or updating the embed. The view's ``_message`` is already set when
        this is called.

        .. warning::
            If this method raises an exception, the view will be unregistered
            from CascadeUI's state system but will remain in discord.py's
            internal view store (there is no public API to remove it). Avoid
            raising from this method unless recovery is not possible.

        Args:
            bot: The discord.py Bot instance.
        """
        pass


# // ========================================( Restore )======================================== // #


async def setup_persistence(
    bot=None,
    file_path: str = "cascadeui_state.json",
    backend=None,
) -> Dict[str, List[str]]:
    """Enable state persistence and restore saved state from disk.

    Call this once in your bot's ``setup_hook`` (or ``on_ready`` with a
    guard). It handles both data persistence and view re-attachment in
    a single call.

    - **Without** ``bot``: enables data persistence only. Views with a
      ``state_key`` will find their saved data when re-invoked.
    - **With** ``bot``: also re-attaches any ``PersistentView`` instances
      to their original messages so buttons keep working across restarts.

    Args:
        bot: Optional discord.py Bot instance. Required for re-attaching
             PersistentView instances; omit if you only need data persistence.
        file_path: Path to the JSON file for state storage (used when no
                   ``backend`` is provided).
        backend: Optional StorageBackend instance (e.g. SQLiteBackend,
                 RedisBackend). Falls back to FileStorageBackend if omitted.

    Returns:
        A summary dict with ``restored``, ``skipped``, ``failed``, and
        ``removed`` lists of state_keys. Skipped entries (unknown class)
        are kept in state for the next restart; removed entries (deleted
        message/channel) are permanently cleaned up.
    """
    # Validate bot argument early — a wrong type here (e.g. passing a
    # variable that doesn't exist) would otherwise fail deep in discord.py
    # with a confusing error or silently break view restoration.
    if bot is not None:
        from discord.ext.commands import Bot
        if not isinstance(bot, Bot):
            raise TypeError(
                f"setup_persistence() expected a discord.py Bot instance for 'bot', "
                f"got {type(bot).__name__}. If calling from setup_hook, use 'self' "
                f"(the bot instance), not an undefined variable."
            )

    store = get_store()

    # Enable persistence if not already set up
    if not store.persistence_enabled:
        if backend is None:
            from ..persistence.storage import FileStorageBackend
            backend = FileStorageBackend(file_path)
        store.enable_persistence(backend)

    # Restore state from disk
    await store.restore_state()

    # If no bot was provided, we're done — data persistence is active
    if bot is None:
        logger.info("Persistence enabled (data only, no bot provided)")
        return {"restored": [], "skipped": [], "failed": [], "removed": []}

    registry = store.state.get("persistent_views", {})
    if not registry:
        logger.info("Persistence enabled, no persistent views to restore")
        return {"restored": [], "skipped": [], "failed": [], "removed": []}

    restored = []
    failed = []
    skipped = []
    removed = []

    for state_key, entry in list(registry.items()):
        class_name = entry.get("class_name")
        message_id = entry.get("message_id")
        channel_id = entry.get("channel_id")

        # Check if the view class is registered — a missing class usually means
        # the module wasn't imported yet, so we skip without removing the entry.
        # The entry stays in state so it can be restored on the next restart
        # once the import is fixed.
        view_cls = _persistent_view_classes.get(class_name)
        if view_cls is None:
            logger.warning(
                f"Persistent view class '{class_name}' not found for state_key '{state_key}'. "
                "Make sure the module defining it is imported before calling setup_persistence."
            )
            skipped.append(state_key)
            continue

        # Fetch the channel — NotFound/Forbidden means it's genuinely gone
        try:
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Could not fetch channel {channel_id} for '{state_key}': {e}")
            removed.append(state_key)
            continue

        # Guard against non-messageable channels (e.g. CategoryChannel, ForumChannel)
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning(
                f"Channel {channel_id} for '{state_key}' is not messageable "
                f"({type(channel).__name__}), removing entry"
            )
            removed.append(state_key)
            continue

        # Fetch the message — NotFound means it was deleted while offline
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning(f"Could not fetch message {message_id} for '{state_key}': {e}")
            removed.append(state_key)
            continue

        # Construct the view (no context/interaction needed for restoration)
        view = None
        try:
            view = view_cls(state_key=state_key)

            # Validate custom_ids before attaching — catches broken subclass updates
            view._validate_custom_ids()

            # Set _message directly (not via the property setter) to avoid
            # firing a VIEW_UPDATED dispatch before _register_state runs.
            view._message = message

            # Restore identity fields from saved state so session limiting
            # can index the view properly after restart.
            if entry.get("user_id"):
                view.user_id = int(entry["user_id"])
            if entry.get("guild_id"):
                view.guild_id = int(entry["guild_id"])

            # Re-attach to discord.py's internal view tracking
            bot.add_view(view, message_id=message.id)

            # Register in state system
            await view._register_state()
            store.register_view(view)

            # Let the view do post-restore work
            await view.on_restore(bot)

            restored.append(state_key)
            logger.info(f"Restored persistent view '{state_key}' ({class_name})")

        except Exception as e:
            logger.error(f"Failed to restore persistent view '{state_key}': {e}", exc_info=True)
            failed.append(state_key)
            # Clean up the subscriber, registry, and undo entry that __init__/register registered
            if view is not None:
                store.unsubscribe(view.id)
                store.unregister_view(view.id)
                store._undo_enabled_views.pop(view.id, None)

    # Clean up entries for deleted messages/channels (but not skipped classes)
    if removed:
        for state_key in removed:
            payload = ActionCreators.persistent_view_unregistered(state_key)
            await store.dispatch("PERSISTENT_VIEW_UNREGISTERED", payload)
        logger.info(f"Cleaned up {len(removed)} stale persistent view entries")

    summary = {"restored": restored, "skipped": skipped, "failed": failed, "removed": removed}
    logger.info(f"Persistent view restore complete: {summary}")
    return summary
