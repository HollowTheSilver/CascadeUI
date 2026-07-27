# // ========================================( Modules )======================================== // #


import logging
from typing import Optional

logger = logging.getLogger(__name__)

# // ========================================( Functions )======================================== // #


async def call_hook_safe(
    hook, *args, owner: str = "", log: Optional[logging.Logger] = None
) -> None:
    """Run a fire-and-forget user hook, logging any exception.

    Post-event hooks fire after the state they report has already changed
    but before the render that shows it. An override that raises must not
    take the render down with it, or the cursor advances while the display
    stays put: the page index moves and the page never turns.

    Lives in ``utils`` because both layers need it: the view patterns reach
    it through ``_StatefulMixin._call_hook_safe``, and the V2 composites
    call it directly, since ``components -> views`` is the import direction
    the package does not take.

    Args:
        hook: The bound hook to run.
        *args: Positional arguments for the hook.
        owner: Class name for the log line, so a raising override names the
            surface it came from.
        log: Logger to report through, so failures stay filterable by
            subsystem rather than surfacing under ``cascadeui.utils``.
    """
    try:
        await hook(*args)
    except Exception as exc:
        name = getattr(hook, "__name__", repr(hook))
        where = f" in {owner}" if owner else ""
        (log or logger).warning(f"{name} raised{where}: {exc}")
