# // ========================================( Modules )======================================== // #


import asyncio
import contextlib
import contextvars
import functools
import logging
import sys
import weakref
from typing import Any, Awaitable, Callable, Collection, Coroutine, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("cascadeui.tasks")

# From 3.12, the bound is asyncio.timeout, as wait_for's own is there. Read at
# each call, so a test can take the task path on any interpreter.
_TIMEOUT_CONTEXT = sys.version_info >= (3, 12)

# // ========================================( Classes )======================================== // #


class _TeardownScope:
    """The task running a teardown, and the owners whose cancel of it is held back."""

    __slots__ = ("task", "owed")

    def __init__(self, task: asyncio.Task):
        self.task = task
        self.owed: List[Tuple["TaskManager", str]] = []


_TEARDOWN: contextvars.ContextVar[Optional[_TeardownScope]] = contextvars.ContextVar(
    "cascadeui_teardown", default=None
)


@contextlib.contextmanager
def _teardown_scope():
    """Hold back cancelling the task that runs a teardown until the teardown ends.

    A teardown cancels its owner's tasks, and it can run in one of them: a
    render the view scheduled, or a task the view's own code started.
    Cancelled at once, that task stops at the teardown's next await and the
    final message edit never lands. Inside this scope the cancel waits for
    the scope to close, and a nested scope in the same task joins the
    outermost, so a parent's exit started from a child's task finishes
    before the child's task is cancelled. A task moved to another owner in
    the meantime (a navigation hands it to the destination) is not cancelled.
    """
    task = asyncio.current_task()
    scope = _TEARDOWN.get()
    # A spawned task copies its creator's context, so an inherited scope
    # belongs to another task and does not cover this one.
    if task is None or (scope is not None and scope.task is task):
        yield
        return
    scope = _TeardownScope(task)
    token = _TEARDOWN.set(scope)
    try:
        yield
    finally:
        _TEARDOWN.reset(token)
        for manager, owner_id in scope.owed:
            if manager._owners.get(task) == owner_id:
                task.cancel()
                break


def _in_teardown_scope(method):
    """Run an async teardown method inside :func:`_teardown_scope`."""

    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        with _teardown_scope():
            return await method(*args, **kwargs)

    return wrapper


async def _bounded_wait(awaitable: Awaitable[Any], timeout: Optional[float]) -> Any:
    """Await ``awaitable``, raising ``asyncio.TimeoutError`` after ``timeout`` seconds.

    ``asyncio.wait_for`` on 3.10 and 3.11 returns the result when the caller
    is cancelled just as the work finishes, and the cancel is lost. This keeps
    it: from 3.12 through ``asyncio.timeout``, which ``wait_for`` itself uses
    there, and before 3.12 by running the work as a task, since on 3.11.0 to
    3.11.2 ``asyncio.timeout`` reports its expiry as a cancel in a task that
    has caught one. A timeout cancels the work and waits for it to stop; work
    that finished meanwhile returns its result. On the task path, so does work
    that finished as the caller was cancelled, and the cancel lands at the
    caller's next await, so a lock the work took reaches the caller's cleanup.
    """
    if _TIMEOUT_CONTEXT:
        async with asyncio.timeout(timeout):
            return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait((task,), timeout=timeout)
    except asyncio.CancelledError:
        if task.done() and not task.cancelled():
            asyncio.current_task().cancel()
            return task.result()
        task.cancel()
        await asyncio.wait((task,))
        raise
    if not done:
        task.cancel()
        await asyncio.wait((task,))
        if task.cancelled():
            raise asyncio.TimeoutError
    return task.result()


class TaskManager:
    """Tracks background tasks by owner ID with cancellation on teardown."""

    def __init__(self):
        self._tasks: Dict[str, Set[asyncio.Task]] = {}
        self._owners: Dict[asyncio.Task, str] = {}
        # Held weakly: the loop holds a timer until it fires, and one pending
        # when its loop closes goes with the loop instead of keeping its owner.
        self._timers: Dict[str, "weakref.WeakSet[asyncio.TimerHandle]"] = {}

    def _call_later(
        self, owner_id: str, delay: float, callback: Callable[..., Any], *args: Any
    ) -> asyncio.TimerHandle:
        """Run ``callback(*args)`` after ``delay`` seconds, cancelled with the owner's tasks.

        For a long wait a task would spend asleep: a timer the loop still
        holds when it closes is dropped quietly, where a task still waiting is
        reported as destroyed.
        """

        def fire():
            self._drop_timer(owner_id, handle)
            callback(*args)

        handle = asyncio.get_running_loop().call_later(delay, fire)
        self._timers.setdefault(owner_id, weakref.WeakSet()).add(handle)
        return handle

    def _drop_timer(self, owner_id: str, handle: asyncio.TimerHandle) -> None:
        timers = self._timers.get(owner_id)
        if timers is not None:
            timers.discard(handle)
            if not timers:
                self._timers.pop(owner_id, None)

    def _cancel_timers(self, owner_id: str) -> None:
        for handle in list(self._timers.pop(owner_id, ())):
            handle.cancel()

    def create_task(self, owner_id: str, coro: Coroutine) -> asyncio.Task:
        """Create and track a background task under the given owner ID."""
        task = asyncio.create_task(coro)
        self._tasks.setdefault(owner_id, set()).add(task)
        self._owners[task] = owner_id
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Drop a finished task from tracking and surface any error.

        Runs as a done-callback on the task itself, so cleanup lands one
        event-loop tick after the task finishes. Scheduling the coroutine
        directly (no wrapper) means a cancel-before-first-step closes the
        coroutine through the task rather than orphaning it with a "coroutine
        was never awaited" warning. ``task.exception()`` retrieves the error so
        asyncio does not separately log it as never-retrieved; cancellation is
        not an error and is skipped. The owner is read at completion, since a
        task can change owners while it runs.
        """
        owner_id = self._owners.pop(task, None)
        self._discard(owner_id, task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Task error for owner {owner_id}: {exc}")

    def _discard(self, owner_id: Optional[str], task: asyncio.Task) -> None:
        owned = self._tasks.get(owner_id)
        if owned is not None:
            owned.discard(task)
            if not owned:
                self._tasks.pop(owner_id, None)

    def _transfer(self, task: Optional[asyncio.Task], from_owner: str, to_owner: str) -> bool:
        """Move a running task from one owner to another, if ``from_owner`` holds it."""
        if task is None or task.done() or self._owners.get(task) != from_owner:
            return False
        self._discard(from_owner, task)
        self._tasks.setdefault(to_owner, set()).add(task)
        self._owners[task] = to_owner
        return True

    def _live(self, owner_id: str) -> List[asyncio.Task]:
        """The owner's tasks, after dropping any whose loop has closed.

        A loop closed before a task ran, or before its cleanup did (a test
        runner's, or one a program closed itself), would otherwise leave it
        tracked though it can never run: counted as active, and raising when
        the next loop waits on it.
        """
        tasks = []
        for task in list(self._tasks.get(owner_id, ())):
            if task.get_loop().is_closed():
                self._owners.pop(task, None)
                self._discard(owner_id, task)
            else:
                tasks.append(task)
        return tasks

    def cancel_tasks(self, owner_id: str) -> int:
        """Cancel all tasks and timers for the given owner ID; return the tasks cancelled."""
        self._cancel_timers(owner_id)
        count = 0
        for task in self._live(owner_id):
            if not task.done():
                task.cancel()
                count += 1

        return count

    def _cancel_for_teardown(
        self, owner_id: str, *, keep_caller: bool = False, spare: Collection[asyncio.Task] = ()
    ) -> int:
        """Cancel the owner's tasks, holding back the one running this teardown.

        Behaves as :meth:`cancel_tasks` outside a :func:`_teardown_scope`
        opened in the current task. With ``keep_caller`` the running task is
        left alone entirely, for a teardown the caller goes on to handle: a
        failed send, whose caller is still handling the exception. Tasks in
        ``spare`` are left running too.
        """
        self._cancel_timers(owner_id)
        scope = _TEARDOWN.get()
        current = asyncio.current_task()
        held = scope.task if scope is not None and scope.task is current else None
        count = 0
        for task in self._live(owner_id):
            if task.done():
                continue
            if (keep_caller and task is current) or task in spare:
                continue
            if task is held:
                scope.owed.append((self, owner_id))
                continue
            task.cancel()
            count += 1
        return count

    def get_task_count(self, owner_id: Optional[str] = None) -> int:
        """Get the count of active tasks, optionally filtered by owner."""
        if owner_id is not None:
            return len(self._live(owner_id))

        return sum(len(self._live(owner)) for owner in list(self._tasks))

    async def wait_tasks(self, owner_id: str) -> None:
        """Await all in-flight tasks under the given owner.

        Snapshots the current set so tasks spawned by awaited callbacks do
        not extend the wait indefinitely. Exceptions inside tasks are already
        logged by ``_on_task_done``; this helper swallows them so a single
        failing subscriber does not abort the flush.
        """
        tasks = self._live(owner_id)
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
