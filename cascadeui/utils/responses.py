# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)

# // ========================================( Functions )======================================== // #


def elapsed_since(interaction: discord.Interaction) -> str:
    """Time since the interaction was created, for ack diagnostics.

    Reports against the local clock deliberately, and says so in the
    string. ``created_at`` is decoded from Discord's snowflake, so it
    carries Discord's clock while ``utcnow()`` carries the host's, and
    the difference includes whatever skew sits between them. Discord
    enforces the 3s window on its own clock, so a skewed host changes
    only what the log says. Labelling the number keeps it from
    contradicting the sentence it annotates: a host running behind
    prints an elapsed time under 3s beside a message reporting a missed
    3s deadline, which sends the reader after the wrong cause.

    Degrades to a placeholder rather than raising: a diagnostic must
    never crash the ack path it is reporting on.
    """
    try:
        secs = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        return f"{secs:.2f}s since interaction creation by local clock"
    except Exception:
        return "elapsed unknown"


async def ack_backstop(
    interaction: discord.Interaction,
    delay: float,
    *,
    owner: str,
    log: Optional[logging.Logger] = None,
    ephemeral: bool = False,
    warn_on_expiry: bool = False,
) -> None:
    """Sleep, then acknowledge the interaction if nothing else has.

    Discord drops an interaction that goes unacknowledged for 3 seconds.
    Every surface that runs user code on that clock arms one of these
    before the work starts: view callbacks via ``_scheduled_task``, the
    send pipeline, ``DynamicPersistentButton`` clicks, and
    ``Modal.on_submit``. The caller cancels the task once it has
    responded, so the common path never reaches the ``defer``.

    Args:
        interaction: The interaction to acknowledge.
        delay: Seconds to wait before acking.
        owner: Class name used in log lines, so a missed ack names the
            surface it came from.
        log: Logger to report through. Callers pass their own module
            logger so ack failures stay filterable by subsystem rather
            than all surfacing under ``cascadeui.utils``.
        ephemeral: Ack visibility, so a followup after this defer renders
            the way the caller intended. Only forwarded when ``True``:
            component interactions ignore the flag, and the send path is
            the one place it carries meaning.
        warn_on_expiry: Log a warning with elapsed time when the
            interaction has already expired (10062). Reserved for the
            surfaces where a miss means the event loop was congested
            through the whole 3s window and is worth surfacing; the
            others debug-log, since a miss there is usually a duplicate
            ack that landed elsewhere first.
    """
    log = log or logger
    try:
        await asyncio.sleep(delay)
        if not interaction.response.is_done():
            if ephemeral:
                await interaction.response.defer(ephemeral=True)
            else:
                await interaction.response.defer()
    except asyncio.CancelledError:
        pass
    except discord.NotFound:
        if warn_on_expiry:
            log.warning(
                # The rate-limit wait is named because it is invisible from
                # here: discord.py sleeps on an exhausted bucket inside
                # HTTPClient.request, so the call simply takes longer and
                # nothing distinguishes it from a busy loop. The other two
                # causes both point at the caller's own code, which sends a
                # reader looking there first.
                f"Auto-defer ack missed the 3s deadline in {owner}: "
                f"{elapsed_since(interaction)} (event-loop congestion, slow "
                f"pre-callback work, or a rate-limit wait inside an earlier "
                f"HTTP call)"
            )
        else:
            log.debug(f"Auto-defer found the interaction already expired in {owner}")
    except Exception:
        log.debug(f"Auto-defer failed for interaction in {owner}")


async def trailing_ack(
    interaction: discord.Interaction,
    *,
    owner: str,
    log: Optional[logging.Logger] = None,
) -> None:
    """Acknowledge an interaction the callback finished without answering.

    Runs after user code returns, so the work is already done and Discord
    only needs to be told the click was handled. Without it a callback
    that edits through the channel endpoint (the
    ``dispatch -> on_state_changed -> refresh`` path) leaves the click
    unacknowledged and the user sees "This interaction failed".

    Classifies the two expected failures rather than swallowing them:
    40060 means something else acked first (routinely, the acting-view
    fast path that was cancelled locally after Discord had already
    processed it), and 10062 means the token is gone. Any other status is
    a real ack failure the user saw as a toast, so it warns.
    """
    log = log or logger
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer()
    except discord.NotFound:
        log.debug(f"Post-callback defer hit a dead interaction in {owner} (10062)")
    except discord.HTTPException as e:
        if getattr(e, "code", None) == 40060:
            log.debug(f"Post-callback defer raced an existing ack in {owner} (40060)")
        else:
            log.warning(
                f"Post-callback defer failed in {owner}: "
                f"status={getattr(e, 'status', '?')} code={getattr(e, 'code', '?')} "
                f"({elapsed_since(interaction)})"
            )
    except Exception:
        log.debug(f"Post-callback defer failed in {owner} (interaction may have expired)")


def _rewind_files(kwargs: dict) -> None:
    """Seek any attachment back to its start before a second send attempt.

    A send that reaches the HTTP layer consumes the file's stream. discord.py
    rewinds only on retry attempts of its own loop (``File.reset(seek=False)``
    on the first pass is deliberately a no-op), so a fall-through to the
    followup path would upload zero bytes. Only reached when the first send
    lost the response slot, which is the one case discord.py does not treat as
    a retry.
    """
    for value in (kwargs.get("file"), *(kwargs.get("files") or ())):
        if isinstance(value, discord.File):
            try:
                value.reset(seek=True)
            except Exception:  # a closed or non-seekable stream is unrecoverable
                logger.debug(f"Could not rewind {value.filename!r} for the followup send")


async def open_modal_safe(
    interaction: discord.Interaction,
    modal: discord.ui.Modal,
    *,
    fallback_message: Optional[str] = None,
) -> bool:
    """Send a modal, falling back to an ephemeral reply when the slot is gone.

    ``send_modal`` must be the first response to an interaction and cannot
    follow a defer, so an ack backstop that fires first makes the modal
    impossible to open. The is_done() read and the send are not atomic: a
    backstop armed outside the interaction lock can take the slot between
    them, so both the local guard (``InteractionResponded``) and Discord's
    own report of the same race (HTTP 40060) route to the fallback rather
    than out of the caller's callback.

    Shared by ``_StatefulMixin.open_modal`` and
    ``DynamicPersistentButton.open_modal`` for the same reason
    :func:`respond_safe` is shared: ``components -> views`` is the import
    direction the package does not take.

    Args:
        interaction: The interaction to open the modal on.
        modal: The modal to send.
        fallback_message: Ephemeral text sent when the slot is already gone.

    Returns:
        ``True`` when the modal opened, ``False`` when the fallback fired.

    Raises:
        ValueError: The modal has no components. Discord rejects a
            zero-component modal with HTTP 400; this converts it into a
            directed build-time error.
    """
    if not modal.children:
        raise ValueError(
            f"Modal {modal.title!r} has no components. A modal needs at "
            f"least one input; Discord rejects an empty one with HTTP 400.\n"
            f"  Fix: pass at least one TextInput to Modal(inputs=[...]), or "
            f"add one via modal.add_item() before opening it."
        )
    if not interaction.response.is_done():
        try:
            await interaction.response.send_modal(modal)
            return True
        except discord.InteractionResponded:
            pass
        except discord.HTTPException as e:
            if getattr(e, "code", None) != 40060:
                raise
    msg = fallback_message or "Could not open the dialog. Please try again."
    await interaction.followup.send(msg, ephemeral=True)
    return False


async def respond_safe(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    ephemeral: bool = False,
    **kwargs,
) -> None:
    """Send an interaction response, falling back to followup if already acked.

    Every ack-armed surface in the library eventually needs this: a view
    callback running under ``serialize_interactions``, a
    ``DynamicPersistentButton`` click, a ``Modal.on_submit`` override, or a
    ``RolesLayoutView`` hook classmethod with no instance to reach a method
    on. In each case an auto-defer timer may have consumed the response slot
    before the reply is written, and a bare
    ``interaction.response.send_message`` then raises
    ``InteractionResponded``. This checks ``interaction.response.is_done()``
    and routes to ``interaction.followup.send`` when the slot is gone.

    Lives in ``utils`` rather than beside ``_StatefulMixin.respond`` because
    ``components/`` consumes it too, and ``components -> views`` is the
    import direction the package does not take.

    Args:
        interaction: The interaction to respond to.
        content: Text content of the response.
        ephemeral: Whether the response is visible only to the clicker.
        **kwargs: Forwarded to ``send_message`` / ``followup.send``
            (``embed=``, ``view=``, ``file=``, ...). ``delete_after`` works
            on both paths even though only ``send_message`` takes it
            natively.
    """
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)
            return
        except discord.InteractionResponded:
            # The is_done() read and the send are not atomic. An ack backstop
            # armed outside the interaction lock can take the slot in between,
            # so the check passing does not mean the send will. Falling through
            # to the followup path delivers the same reply either way.
            pass
        except discord.HTTPException as e:
            if getattr(e, "code", None) != 40060:
                raise
            # 40060 is the same race, reported by Discord rather than by
            # discord.py's local guard: the ack landed server-side first.
        _rewind_files(kwargs)

    # ``Webhook.send`` is the one followup path that does not take
    # delete_after, and which path runs is decided by whether the ack
    # backstop fired first, outside the caller's control. Rather than
    # let the same call crash only sometimes, the timer is run here.
    delete_after = kwargs.pop("delete_after", None)
    if delete_after is None:
        await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
        return

    # wait=True is what makes followup.send return the Message the timer
    # below needs a handle on.
    message = await interaction.followup.send(content, ephemeral=ephemeral, wait=True, **kwargs)

    async def _delete_later() -> None:
        # RateLimited is a sibling of HTTPException, not a subclass, so it
        # needs naming; NotFound is a subclass and does not.
        try:
            await asyncio.sleep(delete_after)
            await message.delete()
        except (discord.HTTPException, discord.RateLimited) as e:
            logger.debug(f"delete_after cleanup failed for a followup: {e!r}")

    def _report(task) -> None:
        # Retrieving the exception keeps asyncio's "never retrieved" warning
        # quiet, and logging it keeps the failure visible. An unlogged
        # retrieval would silently swallow anything the except above missed.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug(f"delete_after task failed for a followup: {exc!r}")

    # Deliberately un-owned. The timer's lifetime belongs to the message, not
    # to any view: routing it through TaskManager under a view's id would let
    # that view's exit() cancel the deletion and strand the notice on screen,
    # the opposite of what the caller asked for. Mirrors how discord.py itself
    # implements delete_after on the paths that support it natively.
    asyncio.create_task(_delete_later()).add_done_callback(_report)
