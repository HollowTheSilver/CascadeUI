# // ========================================( Modules )======================================== // #


from contextvars import ContextVar
from itertools import count
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .store import BatchContext

# // ========================================( State )======================================== // #


# The batches open in the CURRENT TASK, outermost first. Membership follows
# the task rather than the store: ``asyncio.create_task`` copies the context,
# so a spawned task inherits the lineage as it stood at spawn, while two tasks
# that each opened a batch never see the other's. Held as a tuple because
# ``ContextVar.set`` rebinding is what isolates a child context; a mutable list
# would propagate by reference and put both tasks back on one buffer, which is
# the shape this module exists to remove.
_ACTIVE_BATCHES: ContextVar[Tuple["BatchContext", ...]] = ContextVar(
    "cascadeui_active_batches", default=()
)

# Monotonic stamp applied to every batched action. A nested batch hands its
# actions to its parent as one block at exit, so buffer order stops matching
# reduction order as soon as a task spawned inside the parent dispatches into
# it while the nested batch is open. Undo merges its per-slot diffs on a
# first-write-wins rule, which is only correct in reduction order.
_batch_sequence = count()


# // ========================================( Functions )======================================== // #


def next_batch_sequence() -> int:
    """Return the next monotonic stamp for a batched entry."""
    return next(_batch_sequence)


def current_batch() -> Optional["BatchContext"]:
    """Return the innermost batch still open in this task, or ``None``.

    Scans outward instead of reading the last entry. A task spawned inside a
    batch inherits the lineage as it stood at spawn time, and those entries
    close on their own schedule, so the last entry may already be closed while
    an outer one is still collecting. The innermost open entry is the batch
    this task's dispatches belong to; once every inherited entry has closed
    there is no batch and the dispatch notifies immediately.
    """
    for batch in reversed(_ACTIVE_BATCHES.get()):
        if not batch.closed:
            return batch
    return None
