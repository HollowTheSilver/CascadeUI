# // ========================================( Modules )======================================== // #


import copy
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .._batching import current_batch, next_commit_sequence
from ..store import _ON_COMMIT, _SOURCE_VIEW
from ..types import Action, StateData

logger = logging.getLogger(__name__)


class _MissingSentinel:
    """Singleton sentinel that preserves identity across copy/deepcopy.

    In an undo diff it marks a slot, or a key inside one, absent before the
    action: UNDO removes it, and REDO removes it when re-applying a state
    that lacked it. The check is identity (``is _MISSING``), which a string
    could not make safely, and ``@cascade_reducer`` deep-copies state: a
    copy that made a new object would be stored as the slot's value.
    """

    _instance: Optional["_MissingSentinel"] = None

    def __new__(cls) -> "_MissingSentinel":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __deepcopy__(self, memo: Any) -> "_MissingSentinel":
        return self

    def __copy__(self) -> "_MissingSentinel":
        return self

    def __repr__(self) -> str:
        return "_MISSING"

    def __bool__(self) -> bool:
        return False


_MISSING: Any = _MissingSentinel()


class _KeyedDiff(dict):
    """Undo entries for keys inside one dict-valued slot: key -> pre-value or ``_MISSING``.

    Marks "restore these keys in the slot" apart from "restore the slot to
    this value", which a plain dict cannot, since a slot's value is usually
    a dict itself. Every user's scoped data shares the ``scoped`` slot, so a
    whole-slot restore would put back other users' keys along with the
    acting view's. ``deepcopy`` keeps the subclass and the ``_MISSING``
    identity inside it.

    ``created`` records that the slot did not exist before the action, so
    restoring its keys removes the slot too once nothing else is left in it.
    """

    def __init__(self, entries=(), *, created: bool = False) -> None:
        super().__init__(entries)
        self.created = created


# Actions that create no undo entry. The prune actions report rows already
# deleted from disk, so restoring them would describe a registry that no longer
# exists, and INSPECTOR_PURGED_STALE drops component and modal entries, not slots.
_SKIP_ACTIONS: Set[str] = {
    "VIEW_CREATED",
    "VIEW_UPDATED",
    "VIEW_DESTROYED",
    "SESSION_CREATED",
    "NAVIGATION_REPLACE",
    "NAVIGATION_PUSH",
    "NAVIGATION_POP",
    "PERSISTENT_VIEW_REGISTERED",
    "PERSISTENT_VIEW_UNREGISTERED",
    "MODAL_SUBMITTED",
    "BATCH_COMPLETE",
    "UNDO",
    "REDO",
    "COMPONENT_INTERACTION",
    "INSPECTOR_PURGED_STALE",
    "APPLICATION_SLOTS_PRUNED",
    "REGISTRY_PRUNED",
}


# // ========================================( Diff Helpers )======================================== // #


def _diff_application_slots(pre: dict, post: dict) -> Dict[str, Any]:
    """Return per-slot undo diff of what pre-state values need restoring.

    For each top-level slot name that differs between ``pre`` and
    ``post``:

    - Slot present in ``pre`` only (deleted by action): diff maps the
      name to a deepcopy of the pre-value. UNDO restores it.
    - Slot present in ``post`` only (added by action): diff maps the
      name to ``_MISSING``. UNDO pops it.
    - Slot in both as a dict, with keys changed: diff maps the name to a
      :class:`_KeyedDiff` of the changed keys only. UNDO restores those
      keys and leaves the slot's other keys as they are.
    - Slot in both but value changed otherwise: diff maps the name to a
      deepcopy of the pre-value. UNDO overwrites.
    - Slot unchanged: omitted entirely. Other views' concurrent writes
      to their own slots, or to other keys of a shared dict slot, survive
      this view's undo.

    The pairing with :func:`_build_inverse_diff` in the reducer closes
    the loop: when UNDO applies this diff, it captures the current
    post-values for the same slot names into a redo diff, so REDO can
    restore the post-state per-slot without clobbering siblings.
    """
    diff: Dict[str, Any] = {}
    for name in set(pre) | set(post):
        entry = _diff_value(pre.get(name, _MISSING), post.get(name, _MISSING))
        if entry is not _UNCHANGED:
            diff[name] = entry
    return diff


# Returned by _diff_value for a value the action left as it was.
_UNCHANGED = object()


def _diff_value(pre_val: Any, post_val: Any) -> Any:
    """The undo entry restoring ``pre_val`` over ``post_val``, or ``_UNCHANGED``.

    A dict on both sides (an absent one reads as empty) records only the keys
    that changed, at every depth, so a bucket several users write, such as a
    guild's scoped data, keeps the other users' keys when one of them undoes.
    """
    # Reducers shallow-spread, so a value the action did not touch is the SAME
    # object on both sides, and comparing it would walk it for nothing.
    if pre_val is post_val:
        return _UNCHANGED
    pre_dict = {} if pre_val is _MISSING else pre_val
    post_dict = {} if post_val is _MISSING else post_val
    if isinstance(pre_dict, dict) and isinstance(post_dict, dict):
        if pre_val is not _MISSING and post_val is not _MISSING and pre_dict == post_dict:
            # A reducer that deep-copies state hands back every dict equal but
            # not identical; walking each one in Python costs 40 times the
            # comparison at a thousand users.
            return _UNCHANGED
        keyed = _diff_keys(pre_dict, post_dict, created=pre_val is _MISSING)
        if keyed or (pre_val is _MISSING) != (post_val is _MISSING):
            return keyed
        return _UNCHANGED
    if pre_val is _MISSING:
        return _MISSING
    if post_val is _MISSING or not _same(pre_val, post_val):
        return copy.deepcopy(pre_val)
    return _UNCHANGED


def _diff_keys(pre: dict, post: dict, *, created: bool) -> _KeyedDiff:
    """The keys of one dict that changed, mapped to their undo entries."""
    keyed = _KeyedDiff(created=created)
    for key in set(pre) | set(post):
        entry = _diff_value(pre.get(key, _MISSING), post.get(key, _MISSING))
        if entry is not _UNCHANGED:
            keyed[key] = entry
    return keyed


def _same(a: Any, b: Any) -> bool:
    """Equal, and of the same type at every level.

    ``1``, ``1.0`` and ``True`` compare equal, but a change between them is
    still a change to undo, and each is saved to disk differently.
    """
    if a != b:
        return False
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return all(_same(a[key], b[key]) for key in a)
    if isinstance(a, (list, tuple)):
        return all(_same(x, y) for x, y in zip(a, b))
    return True


def _merge_first_write(template: Dict[str, Any], name: str, entry: Any) -> None:
    """Fold one action's undo entry for slot ``name`` into a batch's template.

    The earliest entry wins, per slot and per key within a dict slot, since
    it holds the value from before the batch. A whole-slot entry after
    per-key ones carries the slot as the batch's earlier actions left it, so
    their keys are put back into it to reach the slot as the batch found it.
    """
    if name not in template:
        if isinstance(entry, _KeyedDiff):
            # The merge and the prune edit nested entries in place.
            entry = copy.deepcopy(entry)
        template[name] = entry
        return
    held = template[name]
    if not isinstance(held, _KeyedDiff):
        return
    if isinstance(entry, _KeyedDiff):
        for key, pre in entry.items():
            _merge_first_write(held, key, pre)
        return
    if entry is not _MISSING and not isinstance(entry, dict):
        return
    template[name] = _fold(held, entry)


def _fold(held: _KeyedDiff, entry: Any) -> Any:
    """``entry`` with the keys ``held`` recorded put back: the value before the batch."""
    slot = {} if entry is _MISSING else dict(entry)
    for key, pre in held.items():
        if pre is _MISSING:
            slot.pop(key, None)
        elif isinstance(pre, _KeyedDiff):
            now = slot.get(key, _MISSING)
            if now is not _MISSING and not isinstance(now, dict):
                continue
            nested = _fold(pre, now)
            if nested is _MISSING:
                slot.pop(key, None)
            else:
                slot[key] = nested
        else:
            slot[key] = pre
    return _MISSING if held.created and not slot else slot


def _prune_unchanged(keyed: dict, final: Any, keep: Optional[dict] = None) -> None:
    """Drop the entries of ``keyed`` whose value ``final`` already holds.

    A batch that writes a value and then puts it back records a change for
    each action, and an UNDO restoring it would overwrite whatever another
    view wrote there afterwards. An absent ``final`` reads as an empty dict.
    A key ``keep`` names stays: a step between the batch's changes wrote it,
    and undoing that step restores the step's own pre-value, which only the
    batch's entry then takes back to the start.
    """
    final_dict = {} if final is _MISSING else final
    if not isinstance(final_dict, dict):
        return
    for key, pre in list(keyed.items()):
        written = keep.get(key, _UNCHANGED) if keep is not None else _UNCHANGED
        if written is not _UNCHANGED and not (
            isinstance(pre, _KeyedDiff) and isinstance(written, _KeyedDiff)
        ):
            continue
        now = final_dict.get(key, _MISSING)
        if isinstance(pre, _KeyedDiff):
            _prune_unchanged(pre, now, written if isinstance(written, _KeyedDiff) else None)
            # Nothing left to restore once the key's existence also matches.
            if not pre and pre.created == (now is _MISSING):
                del keyed[key]
        elif pre is _MISSING:
            if now is _MISSING:
                del keyed[key]
        elif now is not _MISSING and _same(now, pre):
            del keyed[key]


def _entry_seq(entry: Any) -> int:
    """The commit stamp an undo entry carries, or -1 for an entry without one."""
    return entry.get("seq", -1) if isinstance(entry, dict) else -1


def _restricted(combined: Dict[str, Any], shape: Dict[str, Any]) -> Dict[str, Any]:
    """The entries of ``combined`` at the slots and keys ``shape`` records."""
    out: Dict[str, Any] = {}
    for name, entry in shape.items():
        held = combined.get(name, entry)
        if isinstance(entry, _KeyedDiff):
            # Never widened to a whole-slot restore, which would take back keys
            # the batch did not write.
            if isinstance(held, _KeyedDiff):
                out[name] = _KeyedDiff(_restricted(held, entry), created=held.created)
            else:
                out[name] = entry
        else:
            out[name] = held
    return out


def _with_interleaved(
    shape: Dict[str, Any],
    records: list,
    stack: list,
    open_above: list = (),
    open_beside: list = (),
) -> Dict[str, Any]:
    """A batch's entry, each key restoring its value from before its earliest write.

    A step another task committed between the batch's first and last change
    sits above the batch's entry on the stack, since the entry takes the
    batch's first stamp. Where that step wrote a key before the batch did, the
    batch's recorded pre-value is the step's result, and undoing both would
    end on it; the step's own pre-value is the one to restore. Another batch
    counts as the changes it recorded, each at its own stamp, when its first
    change falls in the window (``open_above``), since its entry sits above
    too.

    A key the batch left as it found it gets no entry, unless any other writer
    changed it inside the window, including a batch that sits below
    (``open_beside`` holds every open batch's changes there): that writer's
    step then relies on this one to put the key back. ``shape`` is edited in
    place when nothing sits above it, as the prune edits the result.
    """
    first, last = records[0][0], records[-1][0]
    final = records[-1][5]
    above, beside = list(open_above), list(open_beside)
    for entry in stack:
        # A batch's entry holds its changes merged at its first stamp, which is
        # not where each was made; they arrive through ``open_above`` and
        # ``open_beside`` instead.
        if not isinstance(entry, dict) or entry.get("batch"):
            continue
        stamp = _entry_seq(entry)
        if first < stamp < last:
            step = (stamp, entry.get("application_slots") or {})
            above.append(step)
            beside.append(step)
    merged = shape
    if above:
        combined: Dict[str, Any] = {}
        timeline = [(record[0], record[2]) for record in records] + above
        for _seq, diff in sorted(timeline, key=lambda item: item[0]):
            for name, value in diff.items():
                _merge_first_write(combined, name, value)
        merged = copy.deepcopy(_restricted(combined, shape))
    written: Dict[str, Any] = {}
    for _seq, diff in beside:
        for name, value in diff.items():
            _merge_first_write(written, name, value)
    _prune_unchanged(merged, final, written or None)
    return merged


# // ========================================( Middleware )======================================== // #


class UndoMiddleware:
    """Middleware that captures state snapshots for undo/redo support.

    Only captures snapshots for views that have ``enable_undo = True``.
    Batched actions produce a single undo entry: a diff is captured per
    action while the batch is open and merged first-write-wins when it
    commits, so the entry restores the state the batch started from.

    Usage:
        from cascadeui import setup_middleware
        from cascadeui.state.middleware import UndoMiddleware

        await setup_middleware(UndoMiddleware())
    """

    def __init__(self) -> None:
        self._store = None

    async def initialize(self, store) -> None:
        """Bind the middleware to its store. Idempotent."""
        self._store = store

    async def __call__(self, action: Action, state: StateData, next_fn: Callable) -> StateData:
        """Record an undo step for the source view's action as its reducer commits.

        The step is a per-slot diff of what the commit changed, plus the
        session's shared_data before it, so slots other views write survive
        this view's undo. It is recorded at the commit rather than when the
        chain returns: middlewares around this one may await, and another
        dispatch may commit meanwhile, so what this one was handed and what
        the chain returns can both hold other writes. Called outside a
        dispatch, it passes the action on and records nothing.
        """
        action_type = action["type"]
        source_id = action.get("source")
        on_commit = _ON_COMMIT.get()
        if on_commit is None or action_type in _SKIP_ACTIONS or not source_id:
            return await next_fn(action, state)
        # Held from here: a push can tear the view down before the commit, or,
        # with a middleware ahead of this one that awaits, before this runs.
        view = self._store._active_views.get(source_id)
        if view is None:
            sent = _SOURCE_VIEW.get()
            view = sent if getattr(sent, "id", None) == source_id else None
        handed_on = view is not None and view._successor is not None and view.enable_undo
        if not (self._source_has_undo(source_id) or handed_on):
            return await next_fn(action, state)

        def record(prior: StateData, committed: StateData) -> None:
            try:
                self._record_commit(source_id, view, prior, committed)
            except Exception:
                logger.exception(
                    f"Undo could not record {action_type} for view {source_id}; "
                    f"the change has no undo step"
                )

        on_commit.append(record)
        try:
            return await next_fn(action, state)
        finally:
            on_commit.remove(record)

    def _record_commit(
        self, source_id: str, view: Any, prior: StateData, committed: StateData
    ) -> None:
        """Record the entry for one commit of ``source_id``'s action.

        Runs as the reducer commits, before anything else can, so entries
        follow the order the store committed, and the diff holds exactly what
        the commit changed against the store's state before it, including
        what a mapping a middleware built changed. The entry goes onto the
        state just committed, or into the batch collecting it.
        """
        pre_application, pre_session_id, pre_shared = self._pre_state(source_id, prior, view)
        post_application = committed.get("application", {})
        diff = _diff_application_slots(pre_application, post_application)
        batch = current_batch()
        if batch is not None:
            # The batch pushes one entry for all its actions when it ends.
            batch.add_undo_record(
                source_id,
                diff,
                pre_session_id,
                pre_shared,
                post_application,
                view,
            )
            return
        if not diff and self._restores_nothing(pre_shared, pre_session_id, committed):
            # An entry that can put nothing back still takes a slot on a
            # bounded stack, and a run of them evicts the history a user wants.
            return
        target = self._undo_target(source_id, view, committed.get("views", {}))
        if target is None:
            return
        snapshot = {
            "application_slots": diff,
            "shared_data": pre_shared,
            "seq": next_commit_sequence(),
        }
        limit = self._get_undo_limit(target)
        new_views = self._views_with_undo_pushed(committed, target, snapshot, limit)
        if new_views is not None:
            self._store.state = {**committed, "views": new_views}

    def _pre_state(
        self, source_id: str, state: StateData, view: Any = None
    ) -> Tuple[dict, Optional[str], dict]:
        """The application slots, session id, and a copy of that session's shared_data.

        The session is read from the source's row, or from ``view`` once a push
        has torn that row down; the view the step goes to shares the session.
        """
        # Held by identity: reducers shallow-spread, so the nested application
        # dict survives the reducer unmutated and the diff can read it back.
        pre_application = state.get("application", {})
        pre_session_id = self._find_session_id(source_id, state) or getattr(
            view, "session_id", None
        )
        pre_shared: dict = {}
        if pre_session_id:
            session = state.get("sessions", {}).get(pre_session_id, {})
            pre_shared = copy.deepcopy(session.get("shared_data", {}))
        return pre_application, pre_session_id, pre_shared

    def _source_has_undo(self, source_id: str) -> bool:
        """Check if the source view has enable_undo set."""
        return source_id in self._store._undo_enabled_views

    def _get_undo_limit(self, source_id: str) -> int:
        """Get the undo stack limit for a given view."""
        return self._store._undo_enabled_views.get(source_id, 20)

    def _find_session_id(self, source_id: str, state: StateData = None) -> Optional[str]:
        """Find the session_id for a given view source_id."""
        target = state if state is not None else self._store.state
        views = target.get("views", {})
        view_data = views.get(source_id, {})
        return view_data.get("session_id")

    def _restores_nothing(
        self, pre_shared: Optional[dict], session_id: Optional[str], state: StateData
    ) -> bool:
        """Whether a snapshot with an empty slot diff would revert anything.

        Only the ``shared_data`` half is left to check, and the question is
        whether it *changed*, not whether it holds anything. Those come apart
        on the case that matters: an action creating a session's first
        shared_data records a pre-value of ``{}``, which is both falsy and
        exactly what an UNDO has to put back. Reading the emptiness instead of
        the change drops that entry and the creation becomes unrevertable.
        """
        post_shared: dict = {}
        if session_id:
            post_shared = state.get("sessions", {}).get(session_id, {}).get("shared_data", {})
        return (pre_shared if pre_shared is not None else {}) == post_shared

    def finalize_batch(
        self,
        records: List[
            Tuple[int, str, Dict[str, Any], Optional[str], Optional[dict], Dict[str, Any], Any]
        ],
    ) -> None:
        """Push a single per-slot undo diff onto each participating view's stack.

        Called by ``BatchContext.__aexit__`` at the outermost exit, an abort
        included, with the per-action records captured while the batch was open, in
        reduction order. Slots, and keys within a dict slot, merge
        first-write-wins, so the template holds each value from before the
        batch first touched it, and every participating view receives that
        template because they all saw the same slot changes roll up at the
        same boundary. The entry takes the stamp of the batch's first change
        and sits on each stack where that change committed, below entries
        other tasks pushed while the batch was open.

        Diffing per action rather than entry-state against live state is what
        keeps concurrent batches apart: live state holds every open batch's
        writes, so a whole-window diff would put one batch's slots into
        another's undo entry and its UNDO would revert them.

        ``shared_data`` is cached per session because session-mates share a
        single shared_data timeline.
        """
        if not records:
            return

        diff_template: Dict[str, Any] = {}
        shared_data_cache: Dict[str, dict] = {}
        sources: Dict[str, Tuple[Any, str]] = {}
        targets: Dict[str, str] = {}

        for _sequence, source_id, diff, session_id, shared, _post, view in records:
            for name, value in diff.items():
                _merge_first_write(diff_template, name, value)
            cache_key = session_id or ""
            sources.setdefault(source_id, (view, cache_key))
            if cache_key not in shared_data_cache:
                shared_data_cache[cache_key] = shared if shared is not None else {}

        live_state = self._store.state
        views = live_state.get("views", {})
        for source_id, (view, cache_key) in sources.items():
            target = self._undo_target(source_id, view, views)
            if target is not None:
                # A view and the one it pushed to can both hold records,
                # and both resolve to the view on screen: one entry.
                targets.setdefault(target, cache_key)

        # Batches still open read these when they place their own steps, since
        # a step that pruned to nothing leaves nothing on the stack.
        for batch in self._store._undo_batches:
            if batch._placing is not records:
                batch._handed.append(records)

        for source_id, cache_key in targets.items():
            try:
                # A copy per view, so one view's entry cannot change another's;
                # ``_MISSING`` stays the same object for the reducer's ``is`` check.
                view_diff = {
                    name: value if value is _MISSING else copy.deepcopy(value)
                    for name, value in diff_template.items()
                }
                stack = live_state.get("views", {}).get(source_id, {}).get("undo_stack", [])
                above, beside = self._open_batch_steps(source_id, records)
                view_diff = _with_interleaved(view_diff, records, stack, above, beside)
                if not view_diff and self._restores_nothing(
                    shared_data_cache[cache_key], cache_key or None, live_state
                ):
                    # Same reasoning as the per-dispatch path: a batch whose
                    # slot writes all pruned, against unchanged shared_data,
                    # has nothing to give back and must not push a bounded
                    # stack.
                    continue
                snapshot = {
                    "application_slots": view_diff,
                    "shared_data": shared_data_cache[cache_key],
                    "seq": records[0][0],
                    # Another batch reads this one's changes while it is open, or
                    # as handed on here, never from this entry (see _with_interleaved).
                    "batch": True,
                }
                limit = self._get_undo_limit(source_id)
                new_views = self._views_with_undo_pushed(live_state, source_id, snapshot, limit)
            except Exception:
                logger.exception(
                    f"Undo could not record a batch for view {source_id}; "
                    f"its changes have no undo step"
                )
                continue
            if new_views is not None:
                # No middleware return to thread here, so the store's state is
                # rebound, never written in place; ``live_state`` follows so the
                # next view reads this push.
                live_state = {**live_state, "views": new_views}
                self._store.state = live_state

    def _open_batch_steps(self, view_id: str, records: list) -> Tuple[list, list]:
        """Changes open batches on ``view_id`` made inside the window of ``records``.

        Returns those of batches whose first change falls in the window, whose
        entries will sit above the one being placed, and those of every open
        batch, including one whose own step is still being placed.
        """
        first, last = records[0][0], records[-1][0]
        above: list = []
        beside: list = []
        placing = next((b for b in self._store._undo_batches if b._placing is records), None)
        handed = placing._handed if placing is not None else []
        for held in handed:
            if not self._store._records_step_on(held, view_id):
                continue
            inside = [(record[0], record[2]) for record in held if first < record[0] < last]
            beside += inside
            if first < held[0][0] < last:
                above += inside
        for batch in self._store._undo_batches:
            held = batch._undo_records or batch._placing
            if not held or held is records or not batch._holds_steps_for(view_id):
                continue
            inside = [(record[0], record[2]) for record in held if first < record[0] < last]
            beside += inside
            if first < held[0][0] < last:
                above += inside
        return above, beside

    def _undo_target(self, source_id: str, view: Any, views: dict) -> Optional[str]:
        """The view whose stack takes an entry for ``source_id``'s change, or ``None``.

        The source view, until a ``push()``, ``pop()``, or ephemeral Continue
        has passed its panel on; then the view on the panel now, which carried
        the stack. That holds while the source's row still exists too, since
        the row is removed after the hand-over and an entry written to it then
        goes with it. A view its bot's close released is left out, as a
        process restart leaves it.
        """
        if source_id in self._store._released_ids:
            return None
        if view is not None and view._successor is not None:
            successor = view._last_successor()
            return successor.id if successor.id in views else None
        return source_id if source_id in views else None

    def _views_with_undo_pushed(
        self, state: StateData, view_id: str, snapshot: dict, limit: int
    ) -> Optional[dict]:
        """Return a fresh ``views`` mapping with ``snapshot`` pushed onto
        ``view_id``'s undo stack, or ``None`` when the view is absent.

        The entry goes below any entry with a later commit stamp, so a batch's
        entry, pushed when the batch ends, sits where its first change
        committed. Every other entry is the newest and lands on top.

        Pure: constructs fresh undo_stack / view / views dicts and returns
        the new ``views`` mapping without touching ``state`` or any nested
        structure. Reducers shallow-spread, so the input view dict and its
        undo_stack list may be shared with prior state versions; rebuilding
        avoids corrupting them. The caller decides how to apply the result:
        the dispatch path writes it onto its own freshly-reduced state
        (``result["views"] = ...``), the batch-commit path rebinds the live
        store reference (``store.state = {**state, "views": ...}``) rather
        than mutating the committed dict in place.
        """
        views = state.get("views", {})
        view = views.get(view_id)
        if view is None:
            return None

        undo_stack = view.get("undo_stack", [])
        at = len(undo_stack)
        while at and _entry_seq(undo_stack[at - 1]) > _entry_seq(snapshot):
            at -= 1
        new_undo_stack = [*undo_stack[:at], snapshot, *undo_stack[at:]]
        if len(new_undo_stack) > limit:
            new_undo_stack = new_undo_stack[-limit:]

        # Clear redo stack on new action (standard undo/redo behavior)
        new_view = {**view, "undo_stack": new_undo_stack, "redo_stack": []}

        logger.debug(
            f"Undo snapshot pushed for view {view_id} " f"(stack depth: {len(new_undo_stack)})"
        )
        return {**views, view_id: new_view}
