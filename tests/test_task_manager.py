"""Tests for TaskManager: tracking, cleanup, cancellation, and error handling.

The manager schedules each coroutine directly and cleans up through a
done-callback, so completion/cancellation cleanup lands one event-loop tick
after the task finishes. Tests await ``asyncio.sleep(0)`` to let that callback
run before asserting on the tracking state.
"""

import asyncio
import gc
import logging
import warnings

from cascadeui.utils.tasks import TaskManager, get_task_manager


class TestTaskTracking:
    """Tasks are tracked while in flight and dropped once they finish."""

    async def test_create_task_is_tracked_while_running(self):
        tm = TaskManager()
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(60)

        task = tm.create_task("owner", work())
        await started.wait()
        assert tm.get_task_count("owner") == 1

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_completed_task_is_cleaned_up(self):
        tm = TaskManager()

        async def work():
            return 42

        task = tm.create_task("owner", work())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run

        assert tm.get_task_count("owner") == 0
        assert task.result() == 42

    async def test_get_task_count_per_owner_and_total(self):
        tm = TaskManager()

        async def work():
            await asyncio.sleep(60)

        tm.create_task("a", work())
        tm.create_task("a", work())
        tm.create_task("b", work())

        assert tm.get_task_count("a") == 2
        assert tm.get_task_count("b") == 1
        assert tm.get_task_count() == 3
        assert tm.get_task_count("missing") == 0

        tm.cancel_tasks("a")
        tm.cancel_tasks("b")
        await asyncio.sleep(0)


class TestCancellation:
    """cancel_tasks cancels in-flight tasks and clears their tracking."""

    async def test_cancel_tasks_cancels_and_cleans_up(self):
        tm = TaskManager()

        async def work():
            await asyncio.sleep(60)

        task = tm.create_task("owner", work())
        await asyncio.sleep(0)  # let it start

        cancelled = tm.cancel_tasks("owner")
        assert cancelled == 1

        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert tm.get_task_count("owner") == 0
        assert task.cancelled()

    async def test_cancel_tasks_unknown_owner_returns_zero(self):
        tm = TaskManager()
        assert tm.cancel_tasks("nobody") == 0

    async def test_cancel_before_first_step_cleans_up(self):
        """A task cancelled before it runs is still cleaned up, and its
        coroutine never executes a single statement."""
        tm = TaskManager()
        ran = False

        async def work():
            nonlocal ran
            ran = True
            await asyncio.sleep(60)

        task = tm.create_task("owner", work())
        task.cancel()  # before the loop ever runs the task
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert ran is False
        assert task.cancelled()
        assert tm.get_task_count("owner") == 0

    async def test_cancel_before_first_step_emits_no_orphan_warning(self):
        """A cancel-before-first-step closes the coroutine through the task,
        not a wrapper, so no 'coroutine was never awaited' RuntimeWarning
        fires."""
        tm = TaskManager()

        async def work():
            await asyncio.sleep(60)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = tm.create_task("owner", work())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            del task
            gc.collect()

        never_awaited = [w for w in caught if "never awaited" in str(w.message)]
        assert never_awaited == []
        assert tm.get_task_count("owner") == 0


class TestErrorHandling:
    """A failing task is logged once and does not break tracking cleanup."""

    async def test_error_in_task_is_logged(self, caplog):
        tm = TaskManager()

        async def boom():
            raise ValueError("kaboom")

        with caplog.at_level(logging.ERROR, logger="cascadeui.tasks"):
            task = tm.create_task("owner", boom())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        assert any("kaboom" in record.message for record in caplog.records)
        assert tm.get_task_count("owner") == 0

    async def test_cancelled_task_is_not_logged_as_error(self, caplog):
        tm = TaskManager()

        async def work():
            await asyncio.sleep(60)

        with caplog.at_level(logging.ERROR, logger="cascadeui.tasks"):
            task = tm.create_task("owner", work())
            await asyncio.sleep(0)
            tm.cancel_tasks("owner")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestWaitTasks:
    """wait_tasks awaits in-flight tasks and swallows their errors."""

    async def test_wait_tasks_awaits_inflight(self):
        tm = TaskManager()
        done = []

        async def work(n):
            await asyncio.sleep(0.01)
            done.append(n)

        tm.create_task("owner", work(1))
        tm.create_task("owner", work(2))

        await tm.wait_tasks("owner")
        assert sorted(done) == [1, 2]

    async def test_wait_tasks_swallows_errors(self):
        tm = TaskManager()

        async def boom():
            raise ValueError("ignored")

        tm.create_task("owner", boom())
        await tm.wait_tasks("owner")  # must not raise

    async def test_wait_tasks_no_tasks_is_noop(self):
        tm = TaskManager()
        await tm.wait_tasks("nobody")  # must not raise


class TestSingleton:
    """get_task_manager returns a process-wide singleton."""

    def test_returns_same_instance(self):
        assert get_task_manager() is get_task_manager()
