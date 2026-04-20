# // ========================================( Modules )======================================== // #


"""ViewStore dispatch tracing for debugging stuck interactions.

discord.py's ``ViewStore`` routes component clicks via a dispatch table
keyed by ``(message_id, component_type, custom_id)``.  When a click
arrives for a component the store has already evicted -- or for one
whose ``_view`` reference was nulled by an unexpected code path -- the
"View interaction referencing unknown view" warning fires and the
click is silently discarded.

This module wraps ``ViewStore.dispatch_view`` to log the full dispatch
table when a miss occurs.  Activated by ``setup_logging(trace=True)``.
"""

import logging
from typing import Optional

import discord
from discord.ui.view import ViewStore

logger = logging.getLogger(__name__)


# // ========================================( ViewStore Tracing )======================================== // #


_original_dispatch_view = None
_views_unavailable_warned = False
_VIEWSTORE_TRACE_ENABLED = False


def is_viewstore_trace_enabled() -> bool:
    """Return whether ``setup_logging(trace=True)`` installed the tracers.

    The dispatch-MISS wrapper in this module and the build-time trace
    blocks in ``views/base.py`` (``clear_items`` restoration summary,
    ``_stabilize_custom_ids`` assignment log) share the ``[viewstore-trace]``
    tag and must share a single on/off switch. Build-time blocks import
    this accessor lazily to gate their ``logger.debug`` calls.
    """
    return _VIEWSTORE_TRACE_ENABLED


def _install_viewstore_trace() -> None:
    """Wrap ``ViewStore.dispatch_view`` to log dispatch state on misses.

    Called internally by ``setup_logging(trace=True)``.
    """
    global _original_dispatch_view, _VIEWSTORE_TRACE_ENABLED

    if _original_dispatch_view is not None:
        return

    _original_dispatch_view = ViewStore.dispatch_view
    _VIEWSTORE_TRACE_ENABLED = True

    def traced_dispatch_view(
        self: ViewStore,
        component_type: int,
        custom_id: str,
        interaction: discord.Interaction,
    ) -> None:
        global _views_unavailable_warned

        if not hasattr(self, "_views"):
            if not _views_unavailable_warned:
                logger.warning(
                    "[viewstore-trace] ViewStore._views unavailable on "
                    "this discord.py version; tracing degraded to no-op"
                )
                _views_unavailable_warned = True
            return _original_dispatch_view(self, component_type, custom_id, interaction)

        message_id: Optional[int] = interaction.message.id if interaction.message else None
        inner_key = (component_type, custom_id)

        item = None
        if message_id is not None:
            item = self._views.get(message_id, {}).get(inner_key)
        if item is None:
            item = self._views.get(None, {}).get(inner_key)

        if item is not None and item.view is None:
            entries = []
            for mid, inner in self._views.items():
                for (ct, cid), it in inner.items():
                    entries.append(
                        f"mid={mid} ct={ct} cid={cid!r} -> "
                        f"{type(it).__name__}({id(it):x}, "
                        f"view={'None' if it.view is None else type(it.view).__name__})"
                    )
            logger.warning(
                f"[viewstore-trace] dispatch_view MISS component_type={component_type} "
                f"interaction.message.id={message_id} custom_id={custom_id!r} "
                f"item_id={id(item):x} item.view=None -- ViewStore contents:\n  "
                + "\n  ".join(entries)
            )

        return _original_dispatch_view(self, component_type, custom_id, interaction)

    ViewStore.dispatch_view = traced_dispatch_view
    logger.info("ViewStore dispatch tracing enabled")


def _uninstall_viewstore_trace() -> None:
    """Restore the original ``ViewStore.dispatch_view``."""
    global _original_dispatch_view, _VIEWSTORE_TRACE_ENABLED
    if _original_dispatch_view is None:
        return
    ViewStore.dispatch_view = _original_dispatch_view
    _original_dispatch_view = None
    _VIEWSTORE_TRACE_ENABLED = False
    logger.info("ViewStore dispatch tracing disabled")
