# // ========================================( Modules )======================================== // #


import asyncio
import logging
from typing import Coroutine, Dict, Optional, Set

logger = logging.getLogger("cascadeui.tasks")

# // ========================================( Classes )======================================== // #


class TaskManager:
    """Tracks background tasks by owner ID with cancellation on teardown."""

    def __init__(self):
        self._tasks: Dict[str, Set[asyncio.Task]] = {}

    def create_task(self, owner_id: str, coro: Coroutine) -> asyncio.Task:
        """Create and track a background task under the given owner ID."""
        task = asyncio.create_task(coro)
        self._tasks.setdefault(owner_id, set()).add(task)
        task.add_done_callback(lambda t: self._on_task_done(owner_id, t))
        return task

    def _on_task_done(self, owner_id: str, task: asyncio.Task) -> None:
        """Drop a finished task from tracking and surface any error.

        Runs as a done-callback on the task itself, so cleanup lands one
        event-loop tick after the task finishes. Scheduling the coroutine
        directly (no wrapper) means a cancel-before-first-step closes the
        coroutine through the task rather than orphaning it with a "coroutine
        was never awaited" warning. ``task.exception()`` retrieves the error so
        asyncio does not separately log it as never-retrieved; cancellation is
        not an error and is skipped.
        """
        owned = self._tasks.get(owner_id)
        if owned is not None:
            owned.discard(task)
            if not owned:
                self._tasks.pop(owner_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Task error for owner {owner_id}: {exc}")

    def cancel_tasks(self, owner_id: str) -> int:
        """Cancel all tasks for the given owner ID."""
        if owner_id not in self._tasks:
            return 0

        count = 0
        for task in list(self._tasks[owner_id]):
            if not task.done():
                task.cancel()
                count += 1

        return count

    def get_task_count(self, owner_id: Optional[str] = None) -> int:
        """Get the count of active tasks, optionally filtered by owner."""
        if owner_id is not None:
            return len(self._tasks.get(owner_id, set()))

        return sum(len(tasks) for tasks in self._tasks.values())

    async def wait_tasks(self, owner_id: str) -> None:
        """Await all in-flight tasks under the given owner.

        Snapshots the current set so tasks spawned by awaited callbacks do
        not extend the wait indefinitely. Exceptions inside tasks are already
        logged by ``_on_task_done``; this helper swallows them so a single
        failing subscriber does not abort the flush.
        """
        tasks = list(self._tasks.get(owner_id, ()))
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


# Singleton instance
_task_manager = None


def get_task_manager():
    """Get the global task manager instance."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
