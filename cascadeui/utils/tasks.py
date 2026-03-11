
# // ========================================( Modules )======================================== // #


import asyncio
import weakref
from typing import Dict, Set, Optional, Coroutine, Any


# // ========================================( Classes )======================================== // #


class TaskManager:
    """Manages and tracks background tasks to ensure proper cleanup."""

    def __init__(self):
        self._tasks: Dict[str, Set[asyncio.Task]] = {}

    def create_task(self, owner_id: str, coro: Coroutine) -> asyncio.Task:
        """Create and track a background task under the given owner ID."""
        task = asyncio.create_task(self._wrap_task(coro, owner_id))

        if owner_id not in self._tasks:
            self._tasks[owner_id] = set()

        self._tasks[owner_id].add(task)
        return task

    async def _wrap_task(self, coro: Coroutine, owner_id: str) -> Any:
        """Wrap a coroutine to remove the task from tracking when complete."""
        try:
            return await coro
        except asyncio.CancelledError:
            # Re-raise cancellation so it's properly handled
            raise
        except Exception as e:
            # Log exception but don't crash
            from ..utils.logging import AsyncLogger
            logger = AsyncLogger(name="cascadeui.tasks", level="ERROR", path="logs", mode="a")
            logger.error(f"Task error for owner {owner_id}: {e}")
            raise
        finally:
            # Remove task from tracking
            if owner_id in self._tasks:
                task = asyncio.current_task()
                if task in self._tasks[owner_id]:
                    self._tasks[owner_id].remove(task)
                # Clean up empty sets
                if not self._tasks[owner_id]:
                    del self._tasks[owner_id]

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


# Singleton instance
_task_manager = None


def get_task_manager():
    """Get the global task manager instance."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
