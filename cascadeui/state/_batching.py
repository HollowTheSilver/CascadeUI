# // ========================================( Modules )======================================== // #


from contextvars import ContextVar
from itertools import count
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .store import BatchContext

# // ========================================( State )======================================== // #


# The batches open in the current task, outermost first. A spawned task copies
# the context and inherits the lineage as it stood; a tuple, because a list would
# be shared by reference and put two tasks back on one buffer.
_ACTIVE_BATCHES: ContextVar[Tuple["BatchContext", ...]] = ContextVar(
    "cascadeui_active_batches", default=()
)

# Stamped as each change commits (batched actions, batch undo records, undo
# entries). A nested batch hands its actions over as one block, so buffer order
# can differ from commit order, which undo's first-write-wins merge needs.
_commit_sequence = count()


# // ========================================( Functions )======================================== // #


def next_commit_sequence() -> int:
    """Return the next monotonic commit stamp."""
    return next(_commit_sequence)


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
