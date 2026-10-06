"""Tests for TaskManager: tracking, cleanup, cancellation, and error handling.

The manager schedules each coroutine directly and cleans up through a
done-callback, so completion/cancellation cleanup lands one event-loop tick
after the task finishes. Tests await ``asyncio.sleep(0)`` to let that callback
run before asserting on the tracking state.
"""

import asyncio
import gc
import logging
import sys
import warnings
import weakref

import pytest

import cascadeui.utils.tasks as tasks_module
from cascadeui.utils.tasks import TaskManager, _bounded_wait, get_task_manager


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

        assert [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and r.name.startswith("cascadeui")
        ] == []


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


class TestOwnershipTransfer:
    """A running task can move to another owner and is cancelled with that one."""

    async def test_a_transferred_task_belongs_to_its_new_owner(self):
        tm = TaskManager()
        task = tm.create_task("a", asyncio.sleep(60))

        assert tm._transfer(task, "a", "b") is True
        assert tm.get_task_count("a") == 0
        assert tm.get_task_count("b") == 1

        tm.cancel_tasks("a")
        await asyncio.sleep(0)
        assert not task.done()
        tm.cancel_tasks("b")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run
        assert tm.get_task_count() == 0

    async def test_a_task_another_owner_holds_is_not_transferred(self):
        tm = TaskManager()
        task = tm.create_task("a", asyncio.sleep(60))

        assert tm._transfer(task, "z", "b") is False
        assert tm.get_task_count("a") == 1

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestTeardownScope:
    """A teardown that cancels its owner's tasks holds back the one running it."""

    async def test_the_running_task_is_cancelled_when_the_scope_closes(self):
        from cascadeui.utils.tasks import _teardown_scope

        tm = TaskManager()
        log = []

        async def teardown():
            with _teardown_scope():
                tm._cancel_for_teardown("owner")
                await asyncio.sleep(0)
                log.append("finished")
            await asyncio.sleep(0)
            log.append("ran on")

        with pytest.raises(asyncio.CancelledError):
            await tm.create_task("owner", teardown())
        assert log == ["finished"]

    async def test_outside_a_scope_the_running_task_is_cancelled_at_once(self):
        tm = TaskManager()
        log = []

        async def teardown():
            tm._cancel_for_teardown("owner")
            await asyncio.sleep(0)
            log.append("finished")

        with pytest.raises(asyncio.CancelledError):
            await tm.create_task("owner", teardown())
        assert log == []

    async def test_a_task_spawned_inside_a_scope_is_not_covered_by_it(self):
        """A spawned task copies its creator's context, scope included."""
        from cascadeui.utils.tasks import _teardown_scope

        tm = TaskManager()
        log = []

        async def spawned():
            tm._cancel_for_teardown("child")
            await asyncio.sleep(0)
            log.append("spawned finished")

        async def teardown():
            with _teardown_scope():
                child = tm.create_task("child", spawned())
                await asyncio.gather(child, return_exceptions=True)

        await asyncio.create_task(teardown())
        assert log == []


class TestTimers:
    """``_call_later`` runs a callback after a delay, and the owner's cancel takes it."""

    async def test_a_timer_runs_its_callback_and_is_no_longer_tracked(self):
        tm = TaskManager()
        fired = asyncio.Event()
        tm._call_later("owner", 0.01, fired.set)
        assert len(tm._timers["owner"]) == 1

        await asyncio.wait_for(fired.wait(), 2)

        assert "owner" not in tm._timers

    @pytest.mark.parametrize("cancel", ["cancel_tasks", "_cancel_for_teardown"])
    async def test_the_owners_cancel_takes_its_timers_only(self, cancel):
        tm = TaskManager()
        fired = []
        mine = tm._call_later("owner", 0.01, fired.append, "mine")
        theirs = tm._call_later("other", 10, fired.append, "theirs")

        getattr(tm, cancel)("owner")
        await asyncio.sleep(0.05)

        assert mine.cancelled()
        assert fired == []
        assert not theirs.cancelled()
        tm.cancel_tasks("other")

    def test_a_timer_pending_when_its_loop_closes_keeps_nothing_alive(self):
        # Held strongly, a timer still waiting when asyncio.run ended kept its
        # callback's owner, a view, alive for as long as the process ran.
        tm = TaskManager()

        class Owner:
            def arm(self):
                pass

        owner = Owner()
        gone = weakref.ref(owner)

        async def schedule():
            tm._call_later("owner", 60, owner.arm)

        asyncio.run(schedule())
        del owner
        gc.collect()

        assert gone() is None
        assert list(tm._timers.get("owner", ())) == []


class TestTasksFromAClosedLoop:
    """A loop that closed before a task ran left it tracked, though it can
    never run: waiting on the owner's tasks from the next loop raised, and the
    task was still counted as active."""

    @staticmethod
    def _left_by_a_closed_loop():
        tm = TaskManager()
        loop = asyncio.new_event_loop()

        async def start():
            tm.create_task("owner", asyncio.sleep(0))

        loop.run_until_complete(start())
        loop.close()
        [left] = tm._tasks["owner"]
        left._log_destroy_pending = False  # it never runs, by design here
        left.get_coro().close()
        return tm

    def test_waiting_on_the_owner_skips_it(self):
        tm = self._left_by_a_closed_loop()

        async def later():
            await asyncio.wait_for(tm.wait_tasks("owner"), 2)

        asyncio.run(later())
        assert tm.get_task_count("owner") == 0

    @pytest.mark.parametrize("owner", ["owner", None])
    def test_it_is_not_counted(self, owner):
        tm = self._left_by_a_closed_loop()

        assert tm.get_task_count(owner) == 0

    @pytest.mark.parametrize("cancel", ["cancel_tasks", "_cancel_for_teardown"])
    def test_it_is_not_counted_as_cancelled(self, cancel):
        tm = self._left_by_a_closed_loop()

        async def later():
            return getattr(tm, cancel)("owner")

        assert asyncio.run(later()) == 0


class TestBoundedWait:
    """The library's bound on its own awaits. ``asyncio.wait_for`` on 3.10 and
    3.11 returned the result when its caller was cancelled as the work
    finished, so a teardown's cancel was lost and the task ran on. Each case
    runs on both ways the bound is taken: ``asyncio.timeout`` from 3.12 and a
    task before it."""

    @pytest.fixture(params=["timeout", "task"])
    def path(self, request, monkeypatch):
        if request.param == "timeout" and sys.version_info < (3, 11):
            pytest.skip("asyncio.timeout needs 3.11")
        monkeypatch.setattr(tasks_module, "_TIMEOUT_CONTEXT", request.param == "timeout")
        return request.param

    async def test_a_cancel_as_the_work_finishes_goes_through(self, path):
        inner = asyncio.get_running_loop().create_future()
        outer = asyncio.ensure_future(_bounded_wait(inner, 10))
        for _ in range(3):
            await asyncio.sleep(0)
        inner.set_result("done")
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    async def test_a_lock_acquire_racing_its_timeout_is_not_left_held(self, path):
        lock = asyncio.Lock()
        await lock.acquire()
        asyncio.get_running_loop().call_later(0.05, lock.release)
        try:
            await _bounded_wait(lock.acquire(), 0.05)
        except asyncio.TimeoutError:
            assert not lock.locked()
        else:
            assert lock.locked()
            lock.release()

    async def test_a_lock_taken_as_the_caller_is_cancelled_is_not_left_held(self, path):
        # On the task path the lock was taken in a task of its own and the
        # cancel dropped it, so nothing released it.
        lock = asyncio.Lock()
        await lock.acquire()

        async def caller():
            await _bounded_wait(lock.acquire(), 10)
            try:
                await asyncio.sleep(10)
            finally:
                lock.release()

        outer = asyncio.ensure_future(caller())
        for _ in range(3):
            await asyncio.sleep(0)
        lock.release()  # hands the lock to the waiting acquire
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert not lock.locked()

    async def test_a_stall_times_out_and_stops_the_work(self, path):
        work = asyncio.ensure_future(asyncio.sleep(10))
        with pytest.raises(asyncio.TimeoutError):
            await _bounded_wait(work, 0.01)
        await asyncio.sleep(0)
        assert work.cancelled()

    async def test_the_works_own_error_comes_through_unchanged(self, path):
        class ConnectTimeout(ConnectionError, asyncio.TimeoutError):
            pass

        async def fail():
            await asyncio.sleep(0)
            raise ConnectTimeout("socket")

        with pytest.raises(ConnectTimeout):
            await _bounded_wait(fail(), 5)

    async def test_work_that_finishes_as_its_timeout_lands_returns_its_result(self, path):
        # A lock acquire that won the race had taken the lock; reporting a
        # timeout left it held with no one to release it.
        async def finishes_anyway():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "finished"

        assert await _bounded_wait(finishes_anyway(), 0.01) == "finished"

    @pytest.mark.skipif(
        not (3, 11) <= sys.version_info < (3, 12),
        reason="3.11.0 to 3.11.2's asyncio.timeout, emulated on the 3.11 line",
    )
    async def test_a_timeout_after_a_caught_cancel_on_3_11_0_is_a_timeout(self, monkeypatch):
        # asyncio.timeout on 3.11.0 to 3.11.2 reported its own expiry as a
        # cancel in a task that had caught one, so a close retried after a
        # cancel raised CancelledError and left its connection open.
        import asyncio.timeouts as timeouts

        async def exit_as_on_3110(self, exc_type, exc_val, exc_tb):
            if self._timeout_handler is not None:
                self._timeout_handler.cancel()
                self._timeout_handler = None
            if self._state is timeouts._State.EXPIRING:
                self._state = timeouts._State.EXPIRED
                if self._task.uncancel() == 0 and exc_type is asyncio.CancelledError:
                    raise TimeoutError
            elif self._state is timeouts._State.ENTERED:
                self._state = timeouts._State.EXITED
            return None

        monkeypatch.setattr(timeouts.Timeout, "__aexit__", exit_as_on_3110)
        outcome = []

        async def retried_after_a_cancel():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass  # caught, as a close's retry does
            try:
                await _bounded_wait(asyncio.sleep(10), 0.01)
            except asyncio.TimeoutError:
                outcome.append("timeout")

        task = asyncio.ensure_future(retried_after_a_cancel())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait({task}, timeout=2)

        assert outcome == ["timeout"]


class TestSingleton:
    """get_task_manager returns a process-wide singleton."""

    def test_returns_same_instance(self):
        assert get_task_manager() is get_task_manager()
