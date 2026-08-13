# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Optional

import aiohttp
import discord

logger = logging.getLogger(__name__)

# Every way a Discord call reports that it did not land. ``RateLimited``
# is a sibling of ``HTTPException`` rather than a subclass, so catching
# the latter alone misses it. It is raised whenever the client was
# built with ``max_ratelimit_timeout`` and a bucket exceeds it, which is
# a supported upstream option, not an exotic one. ``aiohttp.ClientError``
# is the third sibling: a request that never reached Discord raises from
# the transport rather than from discord.py, so a reset connection or a
# dropped keep-alive carries none of the HTTP types. It is the umbrella
# rather than ``OSError`` for two reasons: ``ServerDisconnectedError``
# is a ``ClientError`` and NOT an ``OSError``, and ``asyncio.TimeoutError``
# IS an ``OSError`` from 3.11 but not on 3.10, so an ``OSError`` clause
# would mean different things across the supported interpreters and would
# swallow the ack-deadline timeouts these seams handle separately.
#
# Catch this tuple at any seam that must survive a failed call; catch
# ``InteractionResponded`` separately, since at an ack it means the work
# is already done and at an edit it means to try another endpoint.
DISCORD_CALL_ERRORS = (discord.HTTPException, discord.RateLimited, aiohttp.ClientError)

# // ========================================( Functions )======================================== // #


def describe_discord_error(exc: BaseException) -> str:
    """Name the cause of a failed Discord call in one clause.

    ``RateLimited`` carries ``retry_after`` and neither ``status`` nor
    ``code``, so a log line written for ``HTTPException`` prints two
    question marks and hides the one number that explains the failure.
    A transport error carries neither either, and the exception type is
    the whole diagnosis: a reset connection and a refused one read the
    same as ``status=? code=?``.
    """
    if isinstance(exc, discord.RateLimited):
        return f"rate limited, retry_after={exc.retry_after:.1f}s"
    if isinstance(exc, discord.InteractionResponded):
        return "interaction already acknowledged"
    if isinstance(exc, aiohttp.ClientError):
        return f"transport failure before Discord answered ({type(exc).__name__}: {exc})"
    return f"status={getattr(exc, 'status', '?')} code={getattr(exc, 'code', '?')}"


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
    3s deadline, which sends the operator after the wrong cause.

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

    Three outcomes, each recorded to match what it costs. Cancelled is
    the healthy path and stays silent. Expired means Discord already
    dropped the interaction, and warns or debug-logs per
    ``warn_on_expiry``. Fired means the handler used its whole budget and
    the ack landed anyway, which logs at INFO: nothing broke, so it is
    not a warning, but it is the leading indicator for the surface that
    eventually expires and it is invisible from outside the library.

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
            # The fired case from the docstring, priced inside the guard so
            # the cancelled path never takes a measurement.
            log.info(
                f"Auto-defer backstop acked for {owner} after the handler used its "
                f"full {delay:.2f}s budget: {elapsed_since(interaction)}"
            )
    except asyncio.CancelledError:
        pass
    except discord.NotFound:
        if warn_on_expiry:
            log.warning(
                # The rate-limit wait is named because it is invisible from
                # here: discord.py sleeps on an exhausted bucket inside
                # HTTPClient.request, so the call simply takes longer and
                # nothing distinguishes it from a busy loop. The other two
                # causes both point at the caller's own code, which sends an
                # operator looking there first.
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
    except DISCORD_CALL_ERRORS as e:
        if getattr(e, "code", None) == 40060:
            log.debug(f"Post-callback defer raced an existing ack in {owner} (40060)")
        else:
            # RateLimited lands here rather than in the generic branch below,
            # which called it an expired interaction -- the wrong cause, and
            # the one case where the fix is to slow down rather than to look
            # for a dead token.
            log.warning(
                f"Post-callback defer failed in {owner}: "
                f"{describe_discord_error(e)} ({elapsed_since(interaction)})"
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
        except aiohttp.ClientError as e:
            # The request never reached Discord, so the dialog did not open
            # and the fallback would travel the same broken connection.
            # Report non-delivery rather than raising: the caller is a
            # component callback, where an escaping exception renders an
            # error card over a click that merely needs repeating.
            logger.warning(
                f"Modal {modal.title!r} did not reach Discord: " f"{describe_discord_error(e)}"
            )
            return False
        except discord.HTTPException as e:
            if getattr(e, "code", None) != 40060:
                raise
    msg = fallback_message or "Could not open the dialog. Please try again."
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except aiohttp.ClientError as e:
        logger.warning(f"Modal fallback notice did not reach Discord: {describe_discord_error(e)}")
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
        except aiohttp.ClientError as e:
            # The request never reached Discord, so whether the reply landed
            # is unknowable from here. The followup path is not retried: if
            # the send did arrive, a second copy is worse than the missing
            # one, and this is a transient notice either way. Raising is the
            # worst of the three -- it renders an error card over a reply the
            # user may already be reading.
            logger.warning(f"Reply did not reach Discord: {describe_discord_error(e)}")
            return
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
    try:
        if delete_after is None:
            await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
            return

        # wait=True is what makes followup.send return the Message the timer
        # below needs a handle on.
        message = await interaction.followup.send(content, ephemeral=ephemeral, wait=True, **kwargs)
    except aiohttp.ClientError as e:
        # Same contract as the response path above: a notice that never left
        # the host is logged, not raised into the caller's callback.
        logger.warning(f"Followup reply did not reach Discord: {describe_discord_error(e)}")
        return

    async def _delete_later() -> None:
        # RateLimited and aiohttp's transport errors are siblings of
        # HTTPException, not subclasses, so they need naming; NotFound is a
        # subclass and does not.
        try:
            await asyncio.sleep(delete_after)
            await message.delete()
        except DISCORD_CALL_ERRORS as e:
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
