"""Tests for LoggingMiddleware action-stream emission level + setup_logging wiring.

The configured ``level`` is the EMISSION level of the action stream, not a
threshold, so a DEBUG-configured stream stays out of INFO-level logs and
surfaces only when DEBUG logging is enabled.
"""

import contextlib
import logging
import subprocess
import sys
from logging.handlers import QueueHandler

import cascadeui.utils.logging as log_mod
from cascadeui.state.middleware import LoggingMiddleware
from cascadeui.state.singleton import get_store
from cascadeui.utils.logging import setup_logging

_ACTION = {"type": "FOO", "source": "src", "payload": {"a": 1}}


async def _passthrough(action, state):
    return state


@contextlib.contextmanager
def _isolated_root():
    """Yield the ``cascadeui`` logger with a clean handler slate, then stop
    the queue pipeline and restore the original handlers and level."""
    lg = logging.getLogger("cascadeui")
    saved_handlers = lg.handlers[:]
    saved_level = lg.level
    lg.handlers.clear()
    try:
        yield lg
    finally:
        log_mod._teardown_handlers(lg)
        lg.handlers[:] = saved_handlers
        lg.setLevel(saved_level)


class TestActionEmissionLevel:
    """Each action record is emitted at the configured level."""

    async def test_default_emits_at_info(self, caplog):
        mw = LoggingMiddleware()
        with caplog.at_level(logging.DEBUG, logger="cascadeui.actions"):
            await mw(_ACTION, {}, _passthrough)
        recs = [r for r in caplog.records if r.name == "cascadeui.actions"]
        assert len(recs) == 1
        assert recs[0].levelno == logging.INFO

    async def test_debug_emits_at_debug(self, caplog):
        mw = LoggingMiddleware(level="DEBUG")
        with caplog.at_level(logging.DEBUG, logger="cascadeui.actions"):
            await mw(_ACTION, {}, _passthrough)
        recs = [r for r in caplog.records if r.name == "cascadeui.actions"]
        assert len(recs) == 1
        assert recs[0].levelno == logging.DEBUG

    async def test_warning_emits_at_warning(self, caplog):
        mw = LoggingMiddleware(level="WARNING")
        with caplog.at_level(logging.DEBUG, logger="cascadeui.actions"):
            await mw(_ACTION, {}, _passthrough)
        recs = [r for r in caplog.records if r.name == "cascadeui.actions"]
        assert len(recs) == 1
        assert recs[0].levelno == logging.WARNING

    async def test_debug_stream_hidden_under_info(self, caplog):
        """The point of the fix: a DEBUG-configured action stream is filtered
        when the logger's effective level is INFO -- previously impossible
        because emission was hardcoded to INFO."""
        mw = LoggingMiddleware(level="DEBUG")
        with caplog.at_level(logging.INFO, logger="cascadeui.actions"):
            await mw(_ACTION, {}, _passthrough)
        recs = [r for r in caplog.records if r.name == "cascadeui.actions"]
        assert recs == []

    async def test_construction_does_not_pin_logger_threshold(self):
        """Construction must not call setLevel on the shared cascadeui.actions
        logger -- visibility is the handlers' job, per the library convention."""
        logger = logging.getLogger("cascadeui.actions")
        logger.setLevel(logging.NOTSET)
        LoggingMiddleware(level="WARNING")
        assert logger.level == logging.NOTSET

    async def test_passes_action_through_to_next(self):
        mw = LoggingMiddleware()
        seen = {}

        async def _capture(action, state):
            seen["action"] = action
            return "result"

        result = await mw(_ACTION, {"s": 1}, _capture)
        assert result == "result"
        assert seen["action"] is _ACTION


class TestSetupLoggingActionLevel:
    """setup_logging's ``actions`` knob passes the emission level through."""

    def _installed(self):
        return [m for m in get_store()._middleware if isinstance(m, LoggingMiddleware)]

    def _cleanup(self, saved_level):
        store = get_store()
        store._middleware[:] = [
            m for m in store._middleware if not isinstance(m, LoggingMiddleware)
        ]
        logging.getLogger("cascadeui").setLevel(saved_level)

    def test_actions_true_installs_at_info(self):
        saved = logging.getLogger("cascadeui").level
        self._cleanup(saved)
        try:
            setup_logging(file=False, stream=False, actions=True)
            installed = self._installed()
            assert len(installed) == 1
            assert installed[0]._level == logging.INFO
        finally:
            self._cleanup(saved)

    def test_actions_level_string_passes_through(self):
        saved = logging.getLogger("cascadeui").level
        self._cleanup(saved)
        try:
            setup_logging(file=False, stream=False, actions="DEBUG")
            installed = self._installed()
            assert len(installed) == 1
            assert installed[0]._level == logging.DEBUG
        finally:
            self._cleanup(saved)

    def test_actions_false_skips_install(self):
        saved = logging.getLogger("cascadeui").level
        self._cleanup(saved)
        try:
            setup_logging(file=False, stream=False, actions=False)
            assert self._installed() == []
        finally:
            self._cleanup(saved)


class TestSetupLoggingColor:
    """setup_logging emits plain console output when color is unsupported."""

    def _stream_fmt(self, **kwargs):
        with _isolated_root() as lg:
            setup_logging(file=False, actions=False, **kwargs)
            # The StreamHandler runs on the listener thread; reach it through
            # the QueueHandler's listener reference.
            qh = next(h for h in lg.handlers if isinstance(h, QueueHandler))
            sh = next(h for h in qh.listener.handlers if isinstance(h, logging.StreamHandler))
            return sh.formatter._formatters[logging.INFO]._fmt

    def test_color_false_emits_plain(self):
        assert "\x1b[" not in self._stream_fmt(color=False)

    def test_color_true_emits_ansi(self):
        assert "\x1b[" in self._stream_fmt(color=True)

    def test_auto_detect_follows_stream_capability(self, monkeypatch):
        # color=None delegates to the detector; a non-color stream -> plain.
        import cascadeui.utils.logging as log_mod

        monkeypatch.setattr(log_mod, "_stream_supports_color", lambda stream: False)
        assert "\x1b[" not in self._stream_fmt()

    def test_no_color_env_forces_plain(self, monkeypatch):
        from cascadeui.utils.logging import _stream_supports_color

        monkeypatch.setenv("NO_COLOR", "1")

        class _TTY:
            def isatty(self):
                return True

        assert _stream_supports_color(_TTY()) is False


class TestSetupLoggingAsyncPipeline:
    """setup_logging routes all library logging through a queue + listener thread."""

    def test_queue_handler_attached_and_listener_running(self):
        with _isolated_root() as lg:
            setup_logging(file=False, actions=False)
            queue_handlers = [h for h in lg.handlers if isinstance(h, QueueHandler)]
            assert len(queue_handlers) == 1
            listener = queue_handlers[0].listener
            assert listener is log_mod._queue_listener
            assert listener._thread is not None and listener._thread.is_alive()
            # No sink handler sits on the logger itself -- the I/O handlers
            # belong to the listener thread.
            assert not any(isinstance(h, logging.StreamHandler) for h in lg.handlers)

    def test_record_lands_in_file(self, tmp_path):
        with _isolated_root():
            setup_logging(stream=False, actions=False, path=str(tmp_path))
            logging.getLogger("cascadeui.state.store").info("queued record %d", 7)
            log_mod._queue_listener.queue.join()
            content = next(tmp_path.glob("cascadeui-*.log")).read_text(encoding="utf-8")
            assert "queued record 7" in content
            assert "cascadeui.state.store" in content
            assert "INFO" in content

    def test_stream_output_arrives(self, capsys):
        with _isolated_root():
            setup_logging(file=False, actions=False, color=False)
            logging.getLogger("cascadeui.test").warning("console line")
            log_mod._queue_listener.queue.join()
            assert "console line" in capsys.readouterr().out

    def test_action_stream_rides_the_queue(self, tmp_path):
        # cascadeui.actions is a child of cascadeui, so LoggingMiddleware
        # output propagates into the same queue with no extra wiring.
        with _isolated_root():
            setup_logging(stream=False, actions=False, path=str(tmp_path))
            logging.getLogger("cascadeui.actions").info("ACTION_DISPATCHED")
            log_mod._queue_listener.queue.join()
            content = next(tmp_path.glob("cascadeui-*.log")).read_text(encoding="utf-8")
            assert "ACTION_DISPATCHED" in content

    def test_exception_text_survives_the_queue(self, tmp_path):
        with _isolated_root():
            setup_logging(stream=False, actions=False, path=str(tmp_path))
            try:
                raise ValueError("boom")
            except ValueError:
                logging.getLogger("cascadeui.views.base").error("failed", exc_info=True)
            log_mod._queue_listener.queue.join()
            content = next(tmp_path.glob("cascadeui-*.log")).read_text(encoding="utf-8")
            assert "Traceback" in content
            assert "ValueError: boom" in content

    def test_shutdown_flushes_queued_records(self, tmp_path):
        with _isolated_root():
            setup_logging(stream=False, actions=False, path=str(tmp_path))
            logger = logging.getLogger("cascadeui.flush")
            for i in range(200):
                logger.info("flush record %d", i)
            log_mod._stop_queue_listener()
            content = next(tmp_path.glob("cascadeui-*.log")).read_text(encoding="utf-8")
            assert content.count("flush record") == 200

    def test_atexit_flushes_on_interpreter_exit(self, tmp_path):
        # A subprocess that exits without stopping the listener must still
        # land every record: the atexit hook drains the queue, then
        # logging.shutdown flushes and closes the sink handlers.
        script = (
            "import logging, sys\n"
            "from cascadeui.utils.logging import setup_logging\n"
            "setup_logging(stream=False, actions=False, path=sys.argv[1])\n"
            "logger = logging.getLogger('cascadeui.exit')\n"
            "for i in range(100):\n"
            "    logger.info('exit record %d', i)\n"
        )
        subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=120)
        content = next(tmp_path.glob("cascadeui-*.log")).read_text(encoding="utf-8")
        assert content.count("exit record") == 100

    def test_repeat_setup_does_not_duplicate(self):
        with _isolated_root() as lg:
            setup_logging(file=False, actions=False)
            first_listener = log_mod._queue_listener
            setup_logging(file=False, actions=False)
            queue_handlers = [h for h in lg.handlers if isinstance(h, QueueHandler)]
            assert len(queue_handlers) == 1
            assert len(lg.handlers) == 1
            # The first listener is stopped and replaced, not stacked.
            assert first_listener._thread is None
            assert log_mod._queue_listener is not first_listener
            assert log_mod._queue_listener._thread.is_alive()

    def test_no_sinks_no_queue(self):
        with _isolated_root() as lg:
            setup_logging(file=False, stream=False, actions=False)
            assert lg.handlers == []
            assert log_mod._queue_listener is None

    def test_handler_param_attaches_directly(self):
        with _isolated_root() as lg:
            own = logging.Handler()
            setup_logging(handler=own, actions=False)
            assert own in lg.handlers
            assert not any(isinstance(h, QueueHandler) for h in lg.handlers)
            # A repeat call detaches the previously supplied handler.
            second = logging.Handler()
            setup_logging(handler=second, actions=False)
            assert own not in lg.handlers
            assert lg.handlers == [second]

    def test_reconfigure_from_queue_to_handler(self):
        with _isolated_root() as lg:
            setup_logging(file=False, actions=False)
            own = logging.Handler()
            setup_logging(handler=own, actions=False)
            assert not any(isinstance(h, QueueHandler) for h in lg.handlers)
            assert own in lg.handlers
            assert log_mod._queue_listener is None
