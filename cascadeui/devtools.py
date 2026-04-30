# // ========================================( Modules )======================================== // #


import io
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context
from discord.ui import ActionRow, TextDisplay

from .components.base import StatefulButton, StatefulSelect
from .components.patterns import action_section, alert, card, divider, gap, key_value
from .exceptions import InstanceLimitError
from .state.actions import ActionCreators
from .state.computed import _SENTINEL as _SENTINEL_COMPUTED
from .state.computed import ComputedValue, computed
from .state.singleton import get_store
from .state.store import StateStore
from .views.patterns import TabLayoutView

logger = logging.getLogger(__name__)


# Shared display cap for list-style commands (``views``, ``sessions``).
# Unified so both commands truncate at the same point -- previously
# ``views`` capped at 15 while ``sessions`` capped at 10, which made the
# two commands feel inconsistent on a store with many entries.
_LIST_DISPLAY_CAP = 15


# // ========================================( Store Internals )======================================== // #
#
# The inspector is the library's own debugging surface, so it reaches into
# store state that user code never should. Private access concentrates
# through these helpers so the privacy boundary stays auditable from one
# place -- refactors to store internals change the helpers, not the dozens
# of call sites downstream.


async def _cleanup_ghost_view(store, view_id: str) -> None:
    """Drop a ghost view (state row with no live instance) from the store.

    Three-step mop-up: remove from the active-view registry, remove the
    subscriber slot, dispatch VIEW_DESTROYED so the state-tree reducer
    trims ``state["views"][view_id]`` and session membership. Used when
    an instance dies without routing through ``exit()`` -- the state
    row outlives the object and only the inspector can clean it.
    """
    store._unregister_view(view_id)
    store._unsubscribe(view_id)
    await store.dispatch("VIEW_DESTROYED", ActionCreators.view_destroyed(view_id))


# // ========================================( Computed Aggregations )======================================== // #
#
# Module-level @computed registrations expose the library's derived-state
# API inside its own DevTools. Only pure functions of `state` are eligible;
# store runtime (active-view registry, subscribers, middleware) stays inline
# in the Config tab because it is not part of the Redux state tree.
#
# Consumers subtract the inspector's own contribution (one view, one session)
# at the read site, since @computed has no view context.


@computed(selector=lambda s: len(s.get("views", {})))
def total_views(count: int) -> int:
    """Total registered views in the state tree (including the inspector itself)."""
    return count


@computed(selector=lambda s: len(s.get("sessions", {})))
def total_sessions(count: int) -> int:
    """Total sessions in the state tree (including the inspector's own session)."""
    return count


@computed(selector=lambda s: tuple(sorted(s.get("application", {}).keys())))
def application_keys(keys: tuple) -> list:
    """Sorted list of top-level keys inside ``state["application"]``."""
    return list(keys)


@computed(selector=lambda s: s)
def state_size_bytes(state: dict) -> int:
    """UTF-8 byte length of the JSON-serialized state tree."""
    return len(json.dumps(state, default=str).encode())


# // ========================================( Inspector )======================================== // #


class InspectorView(TabLayoutView):
    """V2 state inspector for browsing the CascadeUI state store.

    Built on TabLayoutView so the inspector exercises the library's own
    V2 component system. Self-filters its own view and session from
    displayed data to avoid observer-effect noise in the inspection output.

    Tabs:
        Overview  -- State tree summary with key counts and size
        Views     -- Active view instances with exit controls
        Sessions  -- Active sessions with clear controls
        History   -- Recent action dispatch log
        Config    -- Reducers, middleware, hooks, persistence status
    """

    instance_limit = 1
    instance_scope = "user_guild"
    instance_policy = "replace"
    # Push/pop routes through ``store.batch()`` which collapses VIEW_CREATED
    # plus VIEW_DESTROYED into one BATCH_COMPLETE action. The navigation
    # families (NAVIGATION_PUSH / NAVIGATION_POP / NAVIGATION_REPLACE) are
    # subscribed explicitly so the selector fires on every transition even
    # when the view count happens to be stable through the batch.
    # VIEW_UPDATED keeps the Views tab's Channel / Msg fields live when the
    # ``_message`` / ``_update_message_state`` contract writes land on
    # existing rows. Session families catch cross-chain session membership
    # changes that don't produce a VIEW_* action.
    subscribed_actions = {
        "VIEW_CREATED",
        "VIEW_UPDATED",
        "VIEW_DESTROYED",
        "NAVIGATION_PUSH",
        "NAVIGATION_POP",
        "NAVIGATION_REPLACE",
        "SESSION_CREATED",
        "SESSION_UPDATED",
    }
    # Overview is the inspector's home tab; pin it alone on row 1 so the
    # remaining five tabs share row 2 in scan order rather than inheriting
    # the default "fill" split (5 on row 1, 1 on row 2) which isolates the
    # least interesting tab on its own line.
    tab_overflow_policy = "pin_first"

    def state_selector(self, state):
        """Return frozensets of filtered view/session signatures.

        Identity tuple, not counts. A counts-based selector short-circuits
        during push/pop batches where one view births and another dies in
        the same synthetic BATCH_COMPLETE action -- the before/after count
        is identical but the ID set changed. Frozensets expose the identity
        diff and drive ``on_state_changed`` through the _notify_subscribers
        selector gate.

        Each view's tuple bundles ``(view_id, message_id, channel_id)`` so
        ``VIEW_UPDATED`` writes from the ``_message`` / ``_update_message_state``
        contract change the selector signature even when the view set is
        otherwise stable. Without those fields the gate equality-skips the
        notification and the Views tab's Channel / Msg columns stay null
        until the user manually refreshes.
        """
        views = frozenset(
            (k, v.get("message_id"), v.get("channel_id"))
            for k, v in state.get("views", {}).items()
            if k != self.id
        )
        sessions = frozenset(k for k in state.get("sessions", {}) if k != self.session_id)
        return (views, sessions)

    def __init__(self, *args, **kwargs):
        tabs = {
            "\U0001f4ca Overview": self.build_overview,
            "\U0001f441\ufe0f Views": self.build_views,
            "\U0001f4c2 Sessions": self.build_sessions,
            "\U0001f4dc History": self.build_history,
            "\u26a1 Performance": self.build_performance,
            "\u2699\ufe0f Config": self.build_config,
        }
        super().__init__(*args, tabs=tabs, **kwargs)

    # // ==================( Filtering )================== // #

    def _filtered_views(self):
        """Return state views excluding the inspector's own entry."""
        views = self.state_store.state.get("views", {})
        return {k: v for k, v in views.items() if k != self.id}

    def _filtered_sessions(self):
        """Return state sessions excluding the inspector's own session."""
        sessions = self.state_store.state.get("sessions", {})
        return {k: v for k, v in sessions.items() if k != self.session_id}

    def _filtered_history(self):
        """Return action history excluding the inspector's own dispatches."""
        return [a for a in self.state_store.history if a.get("source") != self.id]

    def _filtered_active_views(self):
        """Return active view instances excluding the inspector itself."""
        return {k: v for k, v in self.state_store._active_views.items() if k != self.id}

    def _filtered_components(self):
        """Return component entries excluding the inspector's own interactions."""
        components = self.state_store.state.get("components", {})
        return {k: v for k, v in components.items() if v.get("view_id") != self.id}

    def _filtered_modals(self):
        """Return modal entries excluding the inspector's own view."""
        modals = self.state_store.state.get("modals", {})
        return {k: v for k, v in modals.items() if k != self.id}

    # // ==================( Helpers )================== // #

    def _exit_row(self):
        """Build an exit button ActionRow for the bottom of each tab."""
        return ActionRow(self.make_exit_button(label="Close", emoji="\u274c"))

    async def _refresh(self, interaction):
        await self._safe_defer(interaction)
        await self._refresh_tabs()

    def _truncate(self, items, max_len=200):
        """Join items as comma-separated string, truncating if too long."""
        if not items:
            return "None"
        text = ", ".join(str(i) for i in items)
        if len(text) <= max_len:
            return text
        # Find the last complete item that fits
        truncated = text[:max_len].rsplit(", ", 1)[0]
        shown = truncated.count(",") + 1
        remaining = len(items) - shown
        return f"{truncated}, ... +{remaining} more"

    def _format_timestamp(self, timestamp):
        """Extract HH:MM:SS from an ISO timestamp string."""
        if not timestamp or timestamp == "N/A":
            return "N/A"
        if len(timestamp) > 19:
            return timestamp[11:19]
        if len(timestamp) > 11:
            return timestamp[11:]
        return timestamp

    # // ==================( Overview Tab )================== // #

    async def build_overview(self):
        """State tree summary with key counts and estimated size."""
        store = self.state_store
        components = self._filtered_components()
        modals = self._filtered_modals()

        # Total counts come from the module-level @computed aggregations; the
        # inspector subtracts its own contribution only when registered.
        # TabLayoutView.send() calls build_overview() before super().send()
        # dispatches VIEW_CREATED / SESSION_CREATED, so on first render the
        # inspector's own ids are not yet in state -- subtracting 1
        # unconditionally would under-count every other view by one.
        # Probing state membership picks the right subtraction every render.
        state_views = store.state.get("views", {})
        state_sessions = store.state.get("sessions", {})
        self_view_present = self.id in state_views
        self_session_present = self.session_id in state_sessions
        view_count = max(0, store.computed["total_views"] - (1 if self_view_present else 0))
        session_count = max(
            0,
            store.computed["total_sessions"] - (1 if self_session_present else 0),
        )

        stats = {
            "Views": view_count,
            "Sessions": session_count,
            "Components": len(components),
        }
        if modals:
            stats["Modals"] = len(modals)

        overview = card(
            "## State Inspector",
            action_section(
                "Snapshot of the CascadeUI state store",
                label="Refresh",
                callback=self._refresh,
                emoji="\U0001f504",
            ),
            divider(),
            key_value(stats),
            color=discord.Color.blurple(),
        )

        # Application state summary
        app_keys = store.computed["application_keys"]
        history = self._filtered_history()
        size_kb = store.computed["state_size_bytes"] / 1024

        app_info = {
            "App Keys": self._truncate(app_keys) if app_keys else "(empty)",
            "State Size": f"{size_kb:.1f} KB",
            "History Buffer": f"{len(history)}/{store.history_limit}",
        }

        app_card = card(
            "## Application State",
            key_value(app_info),
            color=discord.Color.dark_grey(),
        )

        # Action buttons -- only show purge when non-inspector entries exist
        actions = []
        if components or modals:
            actions.append(
                StatefulButton(
                    label=f"Purge Stale ({len(components) + len(modals)})",
                    style=discord.ButtonStyle.danger,
                    emoji="\U0001f5d1\ufe0f",
                    callback=self._purge_stale,
                )
            )
        if getattr(self.state_store, "persistence_manager", None) is not None:
            actions.append(
                StatefulButton(
                    label="Flush to Disk",
                    style=discord.ButtonStyle.primary,
                    emoji="\U0001f4be",
                    callback=self._flush_to_disk,
                )
            )
        if history:
            actions.append(
                StatefulButton(
                    label="Clear History",
                    style=discord.ButtonStyle.secondary,
                    emoji="\U0001f9f9",
                    callback=self._clear_history,
                )
            )

        items = [overview, gap(), app_card]
        if actions:
            actions.append(self.make_exit_button(label="Close", emoji="\u274c"))
            items.append(ActionRow(*actions))
        else:
            items.append(self._exit_row())
        return items

    async def _purge_stale(self, interaction):
        """Drop orphaned component and modal entries from the state tree.

        Routes through the INSPECTOR_PURGED_STALE reducer instead of
        mutating ``state["components"]`` and ``state["modals"]`` in place.
        The reducer keeps the inspector's own entries (tab-button
        COMPONENT_INTERACTION rows that are expected while the inspector
        is alive) and drops every other entry, mirroring the read-side
        ``_filtered_components`` / ``_filtered_modals`` filters above.
        """
        await self._safe_defer(interaction)
        await self.dispatch("INSPECTOR_PURGED_STALE", {"inspector_id": self.id})
        await self._refresh_tabs()

    async def _flush_to_disk(self, interaction):
        """Force an immediate persistence write."""
        await self._safe_defer(interaction)
        manager = getattr(self.state_store, "persistence_manager", None)
        if manager is not None:
            await manager.flush_all()
            logger.info("Inspector: forced persistence flush")
        await self._refresh_tabs()

    async def _clear_history(self, interaction):
        """Clear the action history buffer."""
        await self._safe_defer(interaction)
        self.state_store.history.clear()
        logger.info("Inspector: cleared action history")
        await self._refresh_tabs()

    # // ==================( Views Tab )================== // #

    async def build_views(self):
        """Active view instances with exit controls."""
        views = self._filtered_views()
        active = self._filtered_active_views()

        if not views:
            empty = alert("No active views", level="info")
            return [empty, self._exit_row()]

        # Build markdown block for view entries
        lines = []
        shown = list(views.items())[:8]
        for view_id, view_data in shown:
            short_id = view_id[:8] + "..."
            view_type = view_data.get("type", "Unknown")
            user_id = view_data.get("user_id", "N/A")
            channel_id = view_data.get("channel_id", "N/A")
            msg_id = view_data.get("message_id", "N/A")
            is_live = "\U0001f7e2" if view_id in active else "\U0001f534"
            lines.append(f"{is_live} **{view_type}** (`{short_id}`)")
            lines.append(f"-# User: {user_id} | Channel: {channel_id} | Msg: {msg_id}")

        if len(views) > 8:
            lines.append(f"\n-# ...showing first 8 of {len(views)}")

        views_card = card(
            "## Active Views",
            TextDisplay(f"{len(views)} view(s) registered"),
            divider(),
            TextDisplay("\n".join(lines)),
            color=discord.Color.green(),
        )

        # Registry stats
        sub_count = len(self.state_store.subscribers) - 1  # Exclude self
        instance_index = self.state_store._instance_index
        registry = card(
            "## View Registry",
            key_value(
                {
                    "Active Instances": len(active),
                    "Instance Index Entries": len(instance_index),
                    "Subscribers": sub_count,
                }
            ),
            color=discord.Color.dark_grey(),
        )

        # View select menu for targeted exit. Use positional indices as
        # SelectOption.value so the wire payload stays inside Discord's
        # 100-char cap regardless of how long the internal view_id is.
        self._view_index_map: dict[str, str] = {}
        options = []
        for idx, (view_id, view_data) in enumerate(shown):
            if not view_id:
                continue
            view_type = view_data.get("type", "Unknown")
            short_id = view_id[:8]
            is_live = view_id in active
            label = f"{view_type} ({short_id}...)"
            desc = "Live instance" if is_live else "Ghost (state only)"
            idx_str = str(idx)
            self._view_index_map[idx_str] = view_id
            options.append(discord.SelectOption(label=label, value=idx_str, description=desc))

        select = StatefulSelect(
            placeholder="Select a view to exit...",
            options=options,
            custom_id="inspector_view_select",
            callback=self._on_view_selected,
        )

        # Action row with bulk controls
        actions = [
            StatefulButton(
                label="Exit Selected",
                style=discord.ButtonStyle.danger,
                emoji="\U0001f6d1",
                callback=self._exit_selected_view,
            ),
            StatefulButton(
                label=f"Exit All ({len(views)})",
                style=discord.ButtonStyle.danger,
                emoji="\u26a0\ufe0f",
                callback=self._exit_all_views,
            ),
            self.make_exit_button(label="Close", emoji="\u274c"),
        ]

        return [
            views_card,
            gap(),
            registry,
            ActionRow(select),
            ActionRow(*actions),
        ]

    async def _on_view_selected(self, interaction):
        """Store the selected view ID for the Exit Selected button."""
        raw = interaction.data.get("values", [None])[0]
        index_map = getattr(self, "_view_index_map", {})
        self._selected_view_id = index_map.get(raw) if raw is not None else None
        await self._safe_defer(interaction)

    async def _exit_selected_view(self, interaction):
        """Exit the view chosen in the select menu."""
        await self._safe_defer(interaction)
        view_id = getattr(self, "_selected_view_id", None)
        if not view_id:
            return await self._refresh_tabs()

        active = self._filtered_active_views()
        if view_id in active:
            try:
                await active[view_id].exit()
                logger.info(f"Inspector: exited live view {view_id[:8]}...")
            except Exception as e:
                logger.warning(f"Inspector: exit failed for {view_id[:8]}...: {e}")
        else:
            await _cleanup_ghost_view(self.state_store, view_id)
            logger.info(f"Inspector: cleaned ghost view {view_id[:8]}...")

        self._selected_view_id = None
        await self._refresh_tabs()

    async def _exit_all_views(self, interaction):
        """Exit all views except the inspector."""
        await self._safe_defer(interaction)
        active = self._filtered_active_views()
        views = self._filtered_views()
        exited = 0

        # Exit live instances first
        for view_id, view in list(active.items()):
            try:
                await view.exit()
                exited += 1
            except Exception as e:
                logger.warning(f"Inspector: exit failed for {view_id[:8]}...: {e}")

        # Clean ghost entries (state but no live instance)
        for view_id in list(views.keys()):
            if view_id not in active:
                await _cleanup_ghost_view(self.state_store, view_id)
                exited += 1

        logger.info(f"Inspector: exited {exited} view(s)")
        await self._refresh_tabs()

    # // ==================( Sessions Tab )================== // #

    async def build_sessions(self):
        """Active sessions with view counts, nav stack info, and clear controls."""
        sessions = self._filtered_sessions()

        if not sessions:
            empty = alert("No active sessions", level="info")
            return [empty, self._exit_row()]

        lines = []
        shown = list(sessions.items())[:6]
        for session_id, session_data in shown:
            member_count = len(session_data.get("members", []))
            created = self._format_timestamp(session_data.get("created_at", "N/A"))
            shared_keys = list(session_data.get("shared_data", {}).keys())

            lines.append(f"**{session_id}**")
            detail = f"-# Members: {member_count} | Created: {created}"
            lines.append(detail)
            if shared_keys:
                lines.append(f"-# Shared Keys: {', '.join(shared_keys[:5])}")
            lines.append("")  # Blank line between entries

        if len(sessions) > 6:
            lines.append(f"-# ...showing first 6 of {len(sessions)}")

        sessions_card = card(
            "## Active Sessions",
            TextDisplay(f"{len(sessions)} session(s)"),
            divider(),
            TextDisplay("\n".join(lines).rstrip()),
            color=discord.Color.gold(),
        )

        # Session select + clear controls. SelectOption.value is capped at
        # 100 chars by Discord; session IDs follow ClassName:user:guild:...
        # patterns that routinely exceed the cap. Ship positional indices
        # on the wire and resolve back to the real session_id at callback
        # time via _session_index_map.
        self._session_index_map: dict[str, str] = {}
        options = []
        for idx, (session_id, session_data) in enumerate(shown):
            if not session_id:
                continue
            member_count = len(session_data.get("members", []))
            label = session_id[:100]  # SelectOption label max
            idx_str = str(idx)
            self._session_index_map[idx_str] = session_id
            options.append(
                discord.SelectOption(
                    label=label,
                    value=idx_str,
                    description=f"{member_count} member(s)",
                )
            )

        select = StatefulSelect(
            placeholder="Select a session to clear...",
            options=options,
            custom_id="inspector_session_select",
            callback=self._on_session_selected,
        )

        actions = [
            StatefulButton(
                label="Clear Selected",
                style=discord.ButtonStyle.danger,
                emoji="\U0001f5d1\ufe0f",
                callback=self._clear_selected_session,
            ),
            self.make_exit_button(label="Close", emoji="\u274c"),
        ]

        return [sessions_card, ActionRow(select), ActionRow(*actions)]

    async def _on_session_selected(self, interaction):
        """Store the selected session ID for the Clear Selected button."""
        raw = interaction.data.get("values", [None])[0]
        index_map = getattr(self, "_session_index_map", {})
        self._selected_session_id = index_map.get(raw) if raw is not None else None
        await self._safe_defer(interaction)

    async def _clear_selected_session(self, interaction):
        """Exit all views in the selected session and remove the session."""
        await self._safe_defer(interaction)
        session_id = getattr(self, "_selected_session_id", None)
        if not session_id:
            return await self._refresh_tabs()

        sessions = self._filtered_sessions()
        session_data = sessions.get(session_id)
        if not session_data:
            self._selected_session_id = None
            return await self._refresh_tabs()

        active = self.state_store._active_views
        exited = 0
        for view_id in list(session_data.get("members", [])):
            if view_id in active:
                try:
                    await active[view_id].exit()
                    exited += 1
                except Exception as e:
                    logger.warning(f"Inspector: exit failed for {view_id[:8]}...: {e}")
            else:
                await _cleanup_ghost_view(self.state_store, view_id)
                exited += 1

        logger.info(f"Inspector: cleared session {session_id} ({exited} view(s) exited)")
        self._selected_session_id = None
        await self._refresh_tabs()

    # // ==================( History Tab )================== // #

    async def build_history(self):
        """Recent action dispatch log."""
        history = self._filtered_history()

        if not history:
            empty = alert("No actions dispatched yet", level="info")
            return [empty, self._exit_row()]

        recent = list(reversed(history[-20:]))
        lines = []
        for action in recent:
            timestamp = self._format_timestamp(action.get("timestamp", ""))
            action_type = action["type"]
            source = action.get("source", "N/A")
            if source and len(source) > 8:
                source = source[:8] + "..."
            lines.append(f"`{timestamp}` **{action_type}** from `{source}`")

        if len(history) > 20:
            lines.append(f"\n-# ...showing last 20 of {len(history)}")

        history_card = card(
            "## Action History",
            action_section(
                f"{len(history)} action(s) in buffer (limit: {self.state_store.history_limit})",
                label="Refresh",
                callback=self._refresh,
                emoji="\U0001f504",
            ),
            divider(),
            TextDisplay("\n".join(lines)),
            color=discord.Color.orange(),
        )

        return [history_card, self._exit_row()]

    # // ==================( Config Tab )================== // #

    async def build_config(self):
        """Reducers, middleware, hooks, and persistence status."""
        store = self.state_store

        # Reducers
        core = list(store._core_reducers.keys()) if store._core_reducers else []
        custom = list(store._custom_reducers.keys())

        reducers_card = card(
            "## Reducers",
            key_value(
                {
                    f"Core ({len(core)})": self._truncate(core),
                    f"Custom ({len(custom)})": self._truncate(custom),
                }
            ),
            color=discord.Color.purple(),
        )

        # Middleware and hooks
        mw_names = []
        for mw in store._middleware:
            if hasattr(mw, "__class__") and mw.__class__.__name__ != "function":
                mw_names.append(mw.__class__.__name__)
            elif hasattr(mw, "__name__"):
                mw_names.append(mw.__name__)
            else:
                mw_names.append("anonymous")

        hook_count = sum(len(cbs) for cbs in store._hooks.values())
        computed_count = len(store._computed)

        middleware_card = card(
            "## Middleware & Hooks",
            key_value(
                {
                    "Middleware": self._truncate(mw_names),
                    "Hooks": f"{hook_count} registered" if hook_count else "None",
                    "Computed Values": f"{computed_count} registered" if computed_count else "None",
                }
            ),
            color=discord.Color.dark_grey(),
        )

        # Persistence -- v3 runs three independent namespaces (registry /
        # application / scoped), each with its own backend or opted out.
        manager = getattr(store, "persistence_manager", None)
        if manager is not None:
            persistent_views = store.state.get("persistent_views", {})
            persist_info = {"Persistent Views": f"{len(persistent_views)} registered"}
            for ns_name, ns_cfg in manager.namespaces.items():
                backend = getattr(ns_cfg, "backend", None)
                persist_info[ns_name.capitalize()] = (
                    backend.__class__.__name__ if backend is not None else "Opted out"
                )
            persist_color = discord.Color.green()
        else:
            persist_info = {"Status": "Disabled"}
            persist_color = discord.Color.red()

        persistence_card = card(
            "## Persistence",
            key_value(persist_info),
            color=persist_color,
        )

        return [reducers_card, gap(), middleware_card, gap(), persistence_card, self._exit_row()]

    # // ==================( Performance Tab )================== // #

    async def _perf_toggle(self, interaction):
        """Flip the store's perf flag on/off."""
        store = self.state_store
        if store._perf_enabled:
            store.disable_perf()
        else:
            store.enable_perf()
        await self._safe_defer(interaction)
        await self._refresh_tabs()

    async def _perf_clear(self, interaction):
        """Drop every recorded sample (dispatch + refresh)."""
        self.state_store.clear_perf()
        await self._safe_defer(interaction)
        await self._refresh_tabs()

    async def _perf_export(self, interaction):
        """Snapshot every sample buffer and send as an ephemeral file.

        The tab displays aggregated views (percentiles + top N) to stay
        within Discord's component and message limits. This handler
        emits the full raw buffers as markdown with a trailing JSON
        appendix, so reviewers receive everything the store captured
        rather than the truncated surface.
        """
        store = self.state_store
        # Snapshot first; the deques may mutate during formatting as
        # background dispatches land. Same self-filter rules as
        # ``build_performance`` so the report matches the tab's view,
        # minus the aggregation cut.
        dispatches = [s for s in store.perf_samples if s["action"] != "COMPONENT_INTERACTION"]
        refreshes = [s for s in store._refresh_samples if s["view_id"] != self.id]
        notifies = [
            s
            for s in store._notify_samples
            if s["subscriber_id"] != self.id and s["action"] != "COMPONENT_INTERACTION"
        ]

        if not dispatches and not refreshes and not notifies:
            await self.respond(
                interaction,
                content="No samples to export -- enable profiling and interact with a view first.",
                ephemeral=True,
            )
            return

        report = self._build_perf_report(dispatches, notifies, refreshes, store._perf_enabled)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"cascadeui-perf-{stamp}.md"
        attachment = discord.File(io.BytesIO(report.encode("utf-8")), filename=filename)

        await self.respond(
            interaction,
            content=(
                f"Performance snapshot -- {len(dispatches)} dispatches, "
                f"{len(notifies)} subscriber samples, {len(refreshes)} refreshes."
            ),
            ephemeral=True,
            file=attachment,
        )

    def _build_perf_report(self, dispatches, notifies, refreshes, enabled):
        """Format the perf buffers as markdown with a trailing JSON appendix.

        Sections mirror the Performance tab's three cards -- Dispatch,
        Subscriber, Refresh -- but emit every sample rather than summary
        percentiles. Raw JSON at the end keeps the report lossless for
        programmatic analysis; ad-hoc tooling can skip the markdown and
        parse the tail fence directly.
        """
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        status = "recording" if enabled else "paused"

        lines = [
            "# CascadeUI Performance Report",
            "",
            f"**Generated:** {generated}",
            f"**Profiling state:** {status}",
            "",
            f"- Dispatch samples: {len(dispatches)}",
            f"- Subscriber samples: {len(notifies)}",
            f"- Refresh samples: {len(refreshes)}",
            "",
            "---",
            "",
        ]

        if dispatches:
            lines.append("## Dispatch Timings")
            lines.append("")
            # Same back-fill as build_performance -- older samples lack
            # ``middleware_ms`` and ``edits``; default to zero/"?".
            filled = [
                {
                    **s,
                    "middleware_ms": s.get("middleware_ms", 0.0),
                    "hooks_ms": s.get("hooks_ms", 0.0),
                }
                for s in dispatches
            ]
            reducer = self._summarize(filled, "reducer_ms")
            middleware = self._summarize(filled, "middleware_ms")
            notify = self._summarize(filled, "notify_ms")
            hooks = self._summarize(filled, "hooks_ms")
            total = self._summarize(filled, "total_ms")

            lines.append("### Aggregates (ms)")
            lines.append("")
            lines.append("| Phase | min | p50 | p95 | max |")
            lines.append("|-------|-----|-----|-----|-----|")
            for label, stats in [
                ("Reducer", reducer),
                ("Middleware", middleware),
                ("Notify", notify),
                ("Hooks", hooks),
                ("Total", total),
            ]:
                lines.append(
                    f"| {label} | {stats['min']:.2f} | {stats['p50']:.2f} | "
                    f"{stats['p95']:.2f} | {stats['max']:.2f} |"
                )
            lines.append("")
            lines.append("### Raw samples (oldest first)")
            lines.append("")
            lines.append(
                "| # | time | action | total | reducer | middleware | notify | hooks | subs | edits |"
            )
            lines.append(
                "|---|------|--------|-------|---------|------------|--------|-------|------|-------|"
            )
            for i, s in enumerate(dispatches):
                t = self._format_timestamp(s["timestamp"])
                mw = s.get("middleware_ms", 0.0)
                hk = s.get("hooks_ms", 0.0)
                edits = s.get("edits", "?")
                lines.append(
                    f"| {i} | {t} | {s['action']} | {s['total_ms']:.2f} | "
                    f"{s['reducer_ms']:.2f} | {mw:.2f} | {s['notify_ms']:.2f} | "
                    f"{hk:.2f} | {s['subscribers']} | {edits} |"
                )
            lines.append("")

        if notifies:
            lines.append("## Subscriber Timings")
            lines.append("")
            notify_stats = self._summarize(notifies, "ms")
            lines.append(
                f"**Overall:** n={notify_stats['count']}, min={notify_stats['min']:.2f}, "
                f"p50={notify_stats['p50']:.2f}, p95={notify_stats['p95']:.2f}, "
                f"max={notify_stats['max']:.2f}"
            )
            lines.append("")
            lines.append("### By subscriber (full list, ranked by p95)")
            lines.append("")
            by_sub = {}
            for s in notifies:
                by_sub.setdefault(s["subscriber_id"], []).append(s["ms"])
            ranked = []
            for sub_id, vals in by_sub.items():
                vals_sorted = sorted(vals)
                p95 = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
                ranked.append((p95, sub_id, vals))
            ranked.sort(key=lambda r: -r[0])
            lines.append("| subscriber_id | n | p95 | max |")
            lines.append("|---------------|---|-----|-----|")
            for p95, sub_id, vals in ranked:
                lines.append(f"| `{sub_id}` | {len(vals)} | {p95:.2f} | {max(vals):.2f} |")
            lines.append("")
            lines.append("### Raw samples (oldest first)")
            lines.append("")
            lines.append("| # | time | subscriber_id | action | ms |")
            lines.append("|---|------|---------------|--------|-----|")
            for i, s in enumerate(notifies):
                t = self._format_timestamp(s["timestamp"])
                lines.append(
                    f"| {i} | {t} | `{s['subscriber_id']}` | {s['action']} | {s['ms']:.2f} |"
                )
            lines.append("")

        if refreshes:
            lines.append("## Refresh Timings")
            lines.append("")
            refresh_stats = self._summarize(refreshes, "refresh_ms")
            lines.append(
                f"**Overall:** n={refresh_stats['count']}, min={refresh_stats['min']:.2f}, "
                f"p50={refresh_stats['p50']:.2f}, p95={refresh_stats['p95']:.2f}, "
                f"max={refresh_stats['max']:.2f}"
            )
            lines.append("")
            lines.append("### By view class (ranked by max)")
            lines.append("")
            by_class = {}
            for s in refreshes:
                by_class.setdefault(s["view_class"], []).append(s["refresh_ms"])
            lines.append("| view_class | n | p95 | max |")
            lines.append("|------------|---|-----|-----|")
            for cls_name, vals in sorted(by_class.items(), key=lambda kv: -max(kv[1])):
                vals_sorted = sorted(vals)
                p95 = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
                lines.append(f"| `{cls_name}` | {len(vals)} | {p95:.2f} | {max(vals):.2f} |")
            lines.append("")
            lines.append("### Raw samples (oldest first)")
            lines.append("")
            lines.append("| # | time | view_class | view_id | ms |")
            lines.append("|---|------|------------|---------|-----|")
            for i, s in enumerate(refreshes):
                t = self._format_timestamp(s["timestamp"])
                vid = s["view_id"][:8] if s.get("view_id") else "?"
                lines.append(
                    f"| {i} | {t} | `{s['view_class']}` | `{vid}` | {s['refresh_ms']:.2f} |"
                )
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Raw JSON")
        lines.append("")
        lines.append("```json")
        payload = {
            "generated_utc": generated,
            "enabled": enabled,
            "dispatches": dispatches,
            "subscribers": notifies,
            "refreshes": refreshes,
        }
        lines.append(json.dumps(payload, indent=2, default=str))
        lines.append("```")

        return "\n".join(lines)

    def _summarize(self, samples, field):
        """Return (min, p50, p95, max) in ms for the given sample field.

        No numpy dependency -- sort + index is clear enough at n<=100.
        """
        if not samples:
            return None
        values = sorted(s[field] for s in samples)
        n = len(values)
        return {
            "count": n,
            "min": values[0],
            "p50": values[n // 2],
            "p95": values[min(n - 1, int(n * 0.95))],
            "max": values[-1],
        }

    async def build_performance(self):
        """Per-dispatch and per-refresh timing samples.

        Reads the store's own profiling hooks. The tab is inert until the
        user flips the toggle; when enabled, every ``dispatch()`` and
        every ``refresh()`` records a sample to a 100-entry ring buffer.
        """
        store = self.state_store
        enabled = store._perf_enabled
        dispatches = [s for s in store.perf_samples if s["action"] != "COMPONENT_INTERACTION"]
        refreshes = [s for s in store._refresh_samples if s["view_id"] != self.id]
        # Subscriber timing is self-filtered the same way refreshes are --
        # the inspector's own ``on_state_changed`` callback would otherwise
        # dominate the list since it fires for every unrelated action.
        notifies = [
            s
            for s in store._notify_samples
            if s["subscriber_id"] != self.id and s["action"] != "COMPONENT_INTERACTION"
        ]

        status_color = discord.Color.green() if enabled else discord.Color.dark_grey()
        status_text = "Recording" if enabled else "Paused"
        toggle_label = "Disable" if enabled else "Enable"
        toggle_emoji = "\u23f8\ufe0f" if enabled else "\u25b6\ufe0f"

        header = card(
            "## Performance Profiling",
            key_value(
                {
                    "Status": status_text,
                    "Dispatch samples": f"{len(dispatches)} / 100",
                    "Refresh samples": f"{len(refreshes)} / 100",
                    "Subscriber samples": f"{len(notifies)} / 500",
                }
            ),
            ActionRow(
                StatefulButton(
                    label=toggle_label,
                    style=(
                        discord.ButtonStyle.primary
                        if not enabled
                        else discord.ButtonStyle.secondary
                    ),
                    emoji=toggle_emoji,
                    callback=self._perf_toggle,
                ),
                StatefulButton(
                    label="Export Report",
                    style=discord.ButtonStyle.success,
                    emoji="\U0001f4e4",
                    callback=self._perf_export,
                ),
                StatefulButton(
                    label="Clear Samples",
                    style=discord.ButtonStyle.secondary,
                    emoji="\U0001f9f9",
                    callback=self._perf_clear,
                ),
                StatefulButton(
                    label="Refresh",
                    style=discord.ButtonStyle.secondary,
                    emoji="\U0001f504",
                    callback=self._refresh,
                ),
            ),
            color=status_color,
        )

        if not dispatches and not refreshes and not notifies:
            hint_level = "info" if enabled else "warning"
            hint_text = (
                "Recording is on. Interact with a view in another channel to collect samples."
                if enabled
                else "Profiling is paused. Toggle Enable to begin sampling."
            )
            return [header, alert(hint_text, level=hint_level), self._exit_row()]

        # Per-dispatch breakdown
        dispatch_card = None
        if dispatches:
            reducer = self._summarize(dispatches, "reducer_ms")
            # ``middleware_ms`` arrived with the measurement split -- older
            # samples captured before the upgrade default to 0 so pre-split
            # dispatches still render without crashing the summary math.
            middleware = self._summarize(
                [{**s, "middleware_ms": s.get("middleware_ms", 0.0)} for s in dispatches],
                "middleware_ms",
            )
            notify = self._summarize(dispatches, "notify_ms")
            total = self._summarize(dispatches, "total_ms")

            def _fmt(stats):
                return (
                    f"min {stats['min']:.2f}  "
                    f"p50 {stats['p50']:.2f}  "
                    f"p95 {stats['p95']:.2f}  "
                    f"max {stats['max']:.2f}"
                )

            recent = list(reversed(dispatches[-10:]))
            recent_lines = []
            for s in recent:
                t = self._format_timestamp(s["timestamp"])
                # "edits" and "middleware_ms" were added in later perf tasks,
                # so samples captured mid-upgrade may lack the keys -- fall
                # back to neutral placeholders rather than crash the tab.
                edits = s.get("edits", "?")
                mw = s.get("middleware_ms", 0.0)
                recent_lines.append(
                    f"`{t}` **{s['action']}** "
                    f"`{s['total_ms']:.2f}ms` "
                    f"(r:{s['reducer_ms']:.2f} / m:{mw:.2f} / n:{s['notify_ms']:.2f}) "
                    f"subs:{s['subscribers']} edits:{edits}"
                )

            dispatch_card = card(
                "## Dispatch Timings (ms)",
                key_value(
                    {
                        f"Reducer  ({total['count']}x)": _fmt(reducer),
                        "Middleware": _fmt(middleware),
                        "Notify": _fmt(notify),
                        "Total": _fmt(total),
                    }
                ),
                divider(),
                TextDisplay("### Recent dispatches\n" + "\n".join(recent_lines)),
                color=discord.Color.blue(),
            )

        # Per-subscriber breakdown -- aggregates the notify_samples ring
        # by subscriber_id, ranks by p95 so the slowest handler rises to
        # the top. Pair with the dispatch card's Notify row: if notify_ms
        # is high and one subscriber dominates this list, that's the
        # candidate to selector-gate or fire-and-forget.
        subscriber_card = None
        if notifies:
            notify_stats = self._summarize(notifies, "ms")
            by_sub = {}
            for s in notifies:
                by_sub.setdefault(s["subscriber_id"], []).append(s["ms"])
            sub_lines = []
            # Rank by p95 descending so the slowest few are visible first.
            ranked = []
            for sub_id, vals in by_sub.items():
                vals_sorted = sorted(vals)
                p95 = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
                ranked.append((p95, sub_id, vals, vals_sorted))
            ranked.sort(key=lambda r: -r[0])
            # Display class name component of the subscriber_id if present
            # (format is typically "ClassName:scope_key"); falls back to
            # raw id otherwise. Truncated to keep each line scannable.
            for p95, sub_id, vals, vals_sorted in ranked[:10]:
                label = sub_id if len(sub_id) <= 40 else sub_id[:37] + "..."
                sub_lines.append(
                    f"`{label}` n={len(vals)} " f"p95={p95:.2f}ms max={max(vals):.2f}ms"
                )
            subscriber_card = card(
                "## Subscriber Timings (ms)",
                key_value(
                    {
                        f"Overall ({notify_stats['count']}x)": (
                            f"min {notify_stats['min']:.2f}  "
                            f"p50 {notify_stats['p50']:.2f}  "
                            f"p95 {notify_stats['p95']:.2f}  "
                            f"max {notify_stats['max']:.2f}"
                        ),
                    }
                ),
                divider(),
                TextDisplay("### By subscriber (top 10 by p95)\n" + "\n".join(sub_lines)),
                color=discord.Color.purple(),
            )

        # Per-refresh breakdown
        refresh_card = None
        if refreshes:
            refresh_stats = self._summarize(refreshes, "refresh_ms")
            # Per-class p95 breakdown -- identifies which view class is slow
            by_class = {}
            for s in refreshes:
                by_class.setdefault(s["view_class"], []).append(s["refresh_ms"])
            class_lines = []
            for cls_name, vals in sorted(by_class.items(), key=lambda kv: -max(kv[1])):
                vals_sorted = sorted(vals)
                p95 = vals_sorted[min(len(vals_sorted) - 1, int(len(vals_sorted) * 0.95))]
                class_lines.append(
                    f"`{cls_name}` n={len(vals)} p95={p95:.2f}ms max={max(vals):.2f}ms"
                )

            refresh_card = card(
                "## Refresh Timings (ms)",
                key_value(
                    {
                        f"Overall ({refresh_stats['count']}x)": (
                            f"min {refresh_stats['min']:.2f}  "
                            f"p50 {refresh_stats['p50']:.2f}  "
                            f"p95 {refresh_stats['p95']:.2f}  "
                            f"max {refresh_stats['max']:.2f}"
                        ),
                    }
                ),
                divider(),
                TextDisplay("### By view class\n" + "\n".join(class_lines)),
                color=discord.Color.teal(),
            )

        children = [header]
        if dispatch_card:
            children.extend([gap(), dispatch_card])
        if subscriber_card:
            children.extend([gap(), subscriber_card])
        if refresh_card:
            children.extend([gap(), refresh_card])
        children.append(self._exit_row())
        return children

    async def on_state_changed(self, state):
        """Auto-refresh the active tab when external state changes."""
        if self.message:
            await self._refresh_tabs()


# // ========================================( DevTools Cog )======================================== // #


class DevToolsCog(commands.Cog, name="cascadeui_devtools"):
    """CascadeUI developer tools cog.

    Provides the visual state inspector and administrative commands
    under a single ``/cascadeui`` command group. All subcommands are
    owner-only and respond ephemerally.

    Usage:
        from cascadeui import DevToolsCog
        await bot.add_cog(DevToolsCog(bot))

    Commands:
        /cascadeui inspect        Open the visual state inspector
        /cascadeui views          List active views
        /cascadeui exit <id>      Exit a view by ID (partial match)
        /cascadeui exitall        Exit all views + clean ghosts
        /cascadeui sessions       List active sessions
        /cascadeui clear <id>     Clear a session by ID
        /cascadeui flush          Force persistence write
        /cascadeui purge          Remove stale component/modal entries
        /cascadeui reset          Reset entire state store (requires confirm)
        /cascadeui persistent     List registered PersistentView classes
        /cascadeui scoped [slot]  Inspect a scoped bucket under state.application
        /cascadeui computed [name] List @computed registrations
        /cascadeui middleware     List installed middleware in dispatch order
        /cascadeui history [n]    Show recent action history
        /cascadeui perf [action]  Toggle perf sampling (on/off/clear/status)
        /cascadeui trace [action] Toggle ViewStore tracing (on/off/status)
        /cascadeui subscribers    List active state subscribers
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="cascadeui", description="CascadeUI developer tools.")
    @commands.is_owner()
    async def cascadeui_group(self, ctx: Context) -> None:
        """CascadeUI developer tools. Run a subcommand for details."""
        lines = ["**CascadeUI DevTools**"]
        for sub in sorted(self.cascadeui_group.commands, key=lambda c: c.name):
            description = sub.description or sub.short_doc or ""
            lines.append(f"`/cascadeui {sub.name}` -- {description}")
        await ctx.send("\n".join(lines), ephemeral=True)

    # // ==================( View Commands )================== // #

    @cascadeui_group.command(name="views", description="List active CascadeUI views.")
    async def list_views(self, ctx: Context) -> None:
        """List all active views in the state store."""
        store = get_store()
        views = store.state.get("views", {})
        active = store.get_active_views()

        if not views:
            return await ctx.send("No active views.", ephemeral=True)

        lines = []
        for view_id, view_data in list(views.items())[:_LIST_DISPLAY_CAP]:
            view_type = view_data.get("type", "Unknown")
            user_id = view_data.get("user_id", "N/A")
            is_live = "\U0001f7e2" if view_id in active else "\U0001f534"
            lines.append(f"{is_live} **{view_type}** `{view_id[:12]}...` (user: {user_id})")

        if len(views) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(views) - _LIST_DISPLAY_CAP} more*")

        header = f"**{len(views)} view(s)** ({len(active)} live)\n"
        await ctx.send(header + "\n".join(lines), ephemeral=True)

    @cascadeui_group.command(name="exit", description="Exit a specific CascadeUI view by ID.")
    async def exit_view(self, ctx: Context, view_id: str) -> None:
        """Exit a specific view. Accepts full or partial (8+ char) IDs."""
        store = get_store()

        # Support partial ID matching
        match = None
        for vid in store.state.get("views", {}):
            if vid == view_id or vid.startswith(view_id):
                if match is not None:
                    return await ctx.send(
                        f"Ambiguous ID `{view_id}` matches multiple views. Use more characters.",
                        ephemeral=True,
                    )
                match = vid

        if not match:
            return await ctx.send(f"No view found matching `{view_id}`.", ephemeral=True)

        view_type = store.state.get("views", {}).get(match, {}).get("type", "Unknown")

        active = store.get_active_views()
        if match in active:
            try:
                await active[match].exit()
                await ctx.send(
                    f"\U0001f7e2 Exited live **{view_type}** (`{match[:12]}...`).", ephemeral=True
                )
            except Exception as e:
                await ctx.send(f"Exit failed: {e}", ephemeral=True)
        else:
            await _cleanup_ghost_view(store, match)
            await ctx.send(
                f"\U0001f534 Cleaned ghost **{view_type}** (`{match[:12]}...`).", ephemeral=True
            )

    @cascadeui_group.command(name="exitall", description="Exit all active CascadeUI views.")
    async def exit_all(self, ctx: Context) -> None:
        """Exit all active views and clean ghost entries."""
        store = get_store()
        views = dict(store.state.get("views", {}))
        active = dict(store.get_active_views())
        exited = 0
        failed = 0

        for view_id, view in list(active.items()):
            try:
                await view.exit()
                exited += 1
            except Exception as exc:
                logger.debug("exit_all: view %s failed to exit cleanly: %s", view_id, exc)
                failed += 1

        for view_id in list(views.keys()):
            if view_id not in active:
                await _cleanup_ghost_view(store, view_id)
                exited += 1

        suffix = f" ({failed} failed)" if failed else ""
        await ctx.send(f"Exited {exited} view(s){suffix}.", ephemeral=True)

    # // ==================( State Commands )================== // #

    @cascadeui_group.command(
        name="flush", description="Force a CascadeUI persistence write to disk."
    )
    async def flush(self, ctx: Context) -> None:
        """Write current state to disk immediately."""
        store = get_store()
        manager = getattr(store, "persistence_manager", None)
        if manager is None:
            return await ctx.send("Persistence is not enabled.", ephemeral=True)

        await manager.flush_all()
        state_json = json.dumps(store.state, default=str)
        size_kb = len(state_json.encode()) / 1024
        await ctx.send(f"\U0001f4be State flushed to disk ({size_kb:.1f} KB).", ephemeral=True)

    @cascadeui_group.command(
        name="purge", description="Remove stale CascadeUI component/modal entries."
    )
    async def purge(self, ctx: Context) -> None:
        """Remove orphaned component and modal interaction data from state.

        Routes through the ``INSPECTOR_PURGED_STALE`` reducer (matches the
        inspector's own Purge Stale button) instead of mutating
        ``state["components"]`` / ``state["modals"]`` in place. Keeps the
        two entry points on one code path and lets middleware observe the
        event. Preserves any live inspector's own rows -- same self-filter
        property the inspector enforces on its Views / Sessions tabs.
        """
        store = get_store()
        components = len(store.state.get("components", {}))
        modals = len(store.state.get("modals", {}))
        total = components + modals

        if not total:
            return await ctx.send("No stale entries to purge.", ephemeral=True)

        inspector_id = next(
            (
                vid
                for vid, view in store.get_active_views().items()
                if isinstance(view, InspectorView)
            ),
            None,
        )
        await store.dispatch("INSPECTOR_PURGED_STALE", {"inspector_id": inspector_id})
        await ctx.send(
            f"\U0001f5d1\ufe0f Purged {components} component(s) and {modals} modal(s).",
            ephemeral=True,
        )

    @cascadeui_group.command(name="reset", description="Reset the entire CascadeUI state store.")
    async def reset(self, ctx: Context, confirm: bool = False) -> None:
        """Nuclear option: reset the state store to its initial empty state.

        Requires ``confirm=True`` to execute. Exits all live views first.
        """
        if not confirm:
            return await ctx.send(
                "\u26a0\ufe0f This will **reset all state** and exit all views.\n"
                "Run `/cascadeui reset confirm:True` to proceed.",
                ephemeral=True,
            )

        store = get_store()

        # Exit all live views. Failures are debug-logged and counted but
        # never block the reset -- a broken view's exit() must not leave
        # state half-torn-down.
        failed = 0
        for view_id, view in list(store.get_active_views().items()):
            try:
                await view.exit()
            except Exception as exc:
                logger.debug("reset: view %s failed to exit cleanly: %s", view_id, exc)
                failed += 1

        # Reset state via the canonical shape helper so new top-level keys
        # added to ``StateStore.__init__`` flow through here automatically.
        store.state = StateStore._build_initial_state()

        # Observability: invalidate every @computed so cached aggregates
        # recompute against the empty state on next access, and clear the
        # selector memo so subscriber selectors do not short-circuit on
        # stale identity comparisons post-reset.
        for cv in store._computed.values():
            if isinstance(cv, ComputedValue):
                cv.invalidate()
        store._last_selected.clear()

        manager = getattr(store, "persistence_manager", None)
        if manager is not None:
            await manager.flush_all()

        suffix = f" ({failed} view exit(s) failed)" if failed else ""
        await ctx.send(
            f"\U0001f4a5 State store reset. All views exited, state cleared.{suffix}",
            ephemeral=True,
        )

    # // ==================( Session Commands )================== // #

    @cascadeui_group.command(name="sessions", description="List active CascadeUI sessions.")
    async def list_sessions(self, ctx: Context) -> None:
        """List all sessions in the state store."""
        store = get_store()
        sessions = store.state.get("sessions", {})

        if not sessions:
            return await ctx.send("No active sessions.", ephemeral=True)

        lines = []
        for session_id, data in list(sessions.items())[:_LIST_DISPLAY_CAP]:
            member_count = len(data.get("members", []))
            lines.append(f"`{session_id}` - {member_count} member(s)")

        if len(sessions) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(sessions) - _LIST_DISPLAY_CAP} more*")

        header = f"**{len(sessions)} session(s)**\n"
        await ctx.send(header + "\n".join(lines), ephemeral=True)

    @cascadeui_group.command(name="clear", description="Clear a specific CascadeUI session by ID.")
    async def clear_session(self, ctx: Context, session_id: str) -> None:
        """Exit all views in a session and remove the session entry."""
        store = get_store()
        sessions = store.state.get("sessions", {})

        if session_id not in sessions:
            return await ctx.send(f"No session found: `{session_id}`.", ephemeral=True)

        session_data = sessions[session_id]
        active = store.get_active_views()
        exited = 0
        failed = 0
        for view_id in list(session_data.get("members", [])):
            if view_id in active:
                try:
                    await active[view_id].exit()
                    exited += 1
                except Exception as exc:
                    logger.debug("clear_session: view %s failed to exit cleanly: %s", view_id, exc)
                    failed += 1
            else:
                await _cleanup_ghost_view(store, view_id)
                exited += 1

        suffix = f", {failed} failed" if failed else ""
        await ctx.send(
            f"\U0001f5d1\ufe0f Cleared session `{session_id}` ({exited} view(s) exited{suffix}).",
            ephemeral=True,
        )

    # // ==================( Inspector Command )================== // #

    @cascadeui_group.command(name="inspect", description="Open the CascadeUI state inspector.")
    async def inspect(self, ctx: Context) -> None:
        """Open the visual state inspector.

        A tabbed V2 dashboard showing the live state tree, active views,
        sessions, action history, and store configuration with interactive
        controls for managing views, sessions, and state.
        """
        try:
            view = InspectorView(context=ctx)
            await view.send()
        except InstanceLimitError:
            await ctx.send("Inspector already open.", ephemeral=True)

    # // ==================( Registry Commands )================== // #

    @cascadeui_group.command(
        name="persistent", description="List registered PersistentView classes."
    )
    async def list_persistent(self, ctx: Context) -> None:
        """Display the module-level registry of PersistentView subclasses.

        The registry maps fully-qualified ``module.QualName`` keys to the
        class object, populated by ``__init_subclass__`` when a module
        defining a persistent view is first imported. Useful for verifying
        that cogs loaded their classes before ``setup_middleware`` ran.
        """
        from .views.persistent import _persistent_view_classes

        if not _persistent_view_classes:
            return await ctx.send("No persistent view classes registered.", ephemeral=True)

        lines = [f"**{len(_persistent_view_classes)} persistent class(es)**"]
        for qualname, cls in list(_persistent_view_classes.items())[:_LIST_DISPLAY_CAP]:
            lines.append(f"`{cls.__name__}` -- {qualname}")
        if len(_persistent_view_classes) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(_persistent_view_classes) - _LIST_DISPLAY_CAP} more*")
        await ctx.send("\n".join(lines), ephemeral=True)

    @cascadeui_group.command(
        name="scoped", description="Inspect a scoped state bucket under state.application."
    )
    async def list_scoped(self, ctx: Context, slot_name: str = "scoped") -> None:
        """Display the keys and sizes of a scoped bucket.

        Walks ``state["application"][slot_name]`` and groups keys by scope
        kind (user / guild / user_guild / global) based on the key shape.
        Unknown shapes are bucketed under ``other`` -- useful for spotting
        keys written by custom reducers that diverged from the library's
        ``_build_scope_key`` convention.
        """
        store = get_store()
        bucket = store.state.get("application", {}).get(slot_name, {})
        if not isinstance(bucket, dict) or not bucket:
            return await ctx.send(
                f"Slot `{slot_name}` is empty or not a scoped bucket.", ephemeral=True
            )

        kinds = {"user": 0, "guild": 0, "user_guild": 0, "global": 0, "other": 0}
        for key in bucket:
            if key == "global":
                kinds["global"] += 1
            elif key.startswith("user:") and ":guild:" in key:
                kinds["user_guild"] += 1
            elif key.startswith("user:"):
                kinds["user"] += 1
            elif key.startswith("guild:"):
                kinds["guild"] += 1
            else:
                kinds["other"] += 1

        lines = [f"**Slot `{slot_name}`: {len(bucket)} entry(ies)**"]
        for kind, count in kinds.items():
            if count:
                lines.append(f"- `{kind}`: {count}")
        for key in list(bucket)[:_LIST_DISPLAY_CAP]:
            value = bucket[key]
            shape = f"dict[{len(value)}]" if isinstance(value, dict) else type(value).__name__
            lines.append(f"`{key}` -- {shape}")
        if len(bucket) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(bucket) - _LIST_DISPLAY_CAP} more*")
        await ctx.send("\n".join(lines), ephemeral=True)

    @cascadeui_group.command(
        name="computed", description="List @computed registrations and their cached values."
    )
    async def list_computed(self, ctx: Context, name: str = None) -> None:
        """List all ``@computed`` values, or detail one by name.

        Without ``name``: shows every registration and whether the cache
        is primed. With ``name``: forces a read against live state and
        returns the current value (primes the cache if empty).
        """
        store = get_store()
        entries = store._computed

        if name is not None:
            cv = entries.get(name)
            if cv is None:
                return await ctx.send(f"No computed named `{name}`.", ephemeral=True)
            value = cv.get(store.state)
            return await ctx.send(f"`{name}` = `{value!r}`", ephemeral=True)

        if not entries:
            return await ctx.send("No @computed values registered.", ephemeral=True)

        lines = [f"**{len(entries)} computed value(s)**"]
        for cv_name, cv in list(entries.items())[:_LIST_DISPLAY_CAP]:
            if isinstance(cv, ComputedValue):
                primed = cv._last_input is not _SENTINEL_COMPUTED
                status = f"cached=`{cv._cached!r}`" if primed else "uncached"
            else:
                status = type(cv).__name__
            lines.append(f"`{cv_name}` -- {status}")
        if len(entries) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(entries) - _LIST_DISPLAY_CAP} more*")
        await ctx.send("\n".join(lines), ephemeral=True)

    @cascadeui_group.command(
        name="middleware", description="List installed middleware in dispatch order."
    )
    async def list_middleware(self, ctx: Context) -> None:
        """Display ``store._middleware`` in pipeline order.

        Middleware runs top-to-bottom between action dispatch and the
        reducer. The class name doubles as the identifier because each
        middleware type is installed at most once by convention.
        """
        store = get_store()
        if not store._middleware:
            return await ctx.send("No middleware installed.", ephemeral=True)

        lines = [f"**{len(store._middleware)} middleware**"]
        for idx, mw in enumerate(store._middleware):
            lines.append(f"{idx + 1}. `{type(mw).__name__}`")
        await ctx.send("\n".join(lines), ephemeral=True)

    # // ==================( Diagnostic Commands )================== // #

    @cascadeui_group.command(name="history", description="Show recent CascadeUI action history.")
    async def show_history(self, ctx: Context, limit: int = 20) -> None:
        """Display the most recent entries in ``store.history``.

        Action history is capped at ``store.history_limit`` (default 100)
        and rotates oldest-first. ``limit`` clamps the response to the
        last N entries and is itself clamped to the cap.
        """
        store = get_store()
        if not store.history:
            return await ctx.send("No actions in history.", ephemeral=True)

        limit = max(1, min(limit, store.history_limit))
        recent = store.history[-limit:]
        lines = [f"**Last {len(recent)} action(s)** (of {len(store.history)})"]
        for action in recent:
            action_type = action.get("type", "?")
            source = action.get("source", "-")
            lines.append(f"`{action_type}` src=`{source}`")
        await ctx.send("\n".join(lines)[:1900], ephemeral=True)

    @cascadeui_group.command(name="perf", description="Toggle or inspect CascadeUI perf sampling.")
    async def toggle_perf(self, ctx: Context, action: str = "status") -> None:
        """Control per-dispatch timing sample collection.

        Actions: ``on`` starts sampling, ``off`` stops (preserves samples),
        ``clear`` drops all samples, ``status`` reports enabled/sample count.
        """
        store = get_store()
        action = action.lower()

        if action == "on":
            store.enable_perf()
            await ctx.send("\U0001f4ca Perf sampling enabled.", ephemeral=True)
        elif action == "off":
            store.disable_perf()
            await ctx.send("\U0001f4ca Perf sampling disabled.", ephemeral=True)
        elif action == "clear":
            store.clear_perf()
            await ctx.send("\U0001f5d1\ufe0f Perf samples cleared.", ephemeral=True)
        elif action == "status":
            enabled = getattr(store, "_perf_enabled", False)
            count = len(getattr(store, "_perf_samples", []))
            state = "enabled" if enabled else "disabled"
            await ctx.send(
                f"\U0001f4ca Perf: {state}, {count} sample(s) retained.",
                ephemeral=True,
            )
        else:
            await ctx.send(
                f"Unknown action `{action}`. Use `on`, `off`, `clear`, or `status`.",
                ephemeral=True,
            )

    @cascadeui_group.command(
        name="trace", description="Toggle CascadeUI ViewStore dispatch tracing."
    )
    async def toggle_trace(self, ctx: Context, action: str = "status") -> None:
        """Control ViewStore dispatch tracing for debugging interaction misses.

        When enabled, the library logs full ViewStore contents on any
        dispatch miss (``item.view is None``) -- the primary diagnostic
        for "interaction referencing unknown view" warnings. Opt-in
        because the log output is noisy on busy bots.
        """
        from .tracing import (
            _install_viewstore_trace,
            _uninstall_viewstore_trace,
            is_viewstore_trace_enabled,
        )

        action = action.lower()
        if action == "on":
            _install_viewstore_trace()
            await ctx.send("\U0001f50d ViewStore tracing enabled.", ephemeral=True)
        elif action == "off":
            _uninstall_viewstore_trace()
            await ctx.send("\U0001f50d ViewStore tracing disabled.", ephemeral=True)
        elif action == "status":
            state = "enabled" if is_viewstore_trace_enabled() else "disabled"
            await ctx.send(f"\U0001f50d ViewStore tracing: {state}.", ephemeral=True)
        else:
            await ctx.send(
                f"Unknown action `{action}`. Use `on`, `off`, or `status`.",
                ephemeral=True,
            )

    @cascadeui_group.command(
        name="subscribers", description="List active state subscribers with filters."
    )
    async def list_subscribers(self, ctx: Context) -> None:
        """Display ``store.subscribers`` with action-filter breakdown.

        Each entry shows the subscriber id (truncated) and how many
        action types it filters on -- ``None`` means the subscriber
        sees every action. Useful for diagnosing "my view stopped
        reacting" bugs: a missing subscriber id explains the silence.
        """
        store = get_store()
        if not store.subscribers:
            return await ctx.send("No subscribers registered.", ephemeral=True)

        lines = [f"**{len(store.subscribers)} subscriber(s)**"]
        for sid, (_, action_filter, _) in list(store.subscribers.items())[:_LIST_DISPLAY_CAP]:
            filter_desc = f"{len(action_filter)} action(s)" if action_filter else "all actions"
            lines.append(f"`{sid[:20]}...` -- {filter_desc}")
        if len(store.subscribers) > _LIST_DISPLAY_CAP:
            lines.append(f"*...and {len(store.subscribers) - _LIST_DISPLAY_CAP} more*")
        await ctx.send("\n".join(lines), ephemeral=True)
