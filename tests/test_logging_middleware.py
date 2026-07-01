"""Tests for LoggingMiddleware action-stream emission level + setup_logging wiring.

The configured ``level`` is the EMISSION level of the action stream, not a
threshold, so a DEBUG-configured stream stays out of INFO-level logs and
surfaces only when DEBUG logging is enabled.
"""

import logging

from cascadeui.state.middleware import LoggingMiddleware
from cascadeui.state.singleton import get_store
from cascadeui.utils.logging import setup_logging

_ACTION = {"type": "FOO", "source": "src", "payload": {"a": 1}}


async def _passthrough(action, state):
    return state


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
        lg = logging.getLogger("cascadeui")
        saved = lg.handlers[:]
        lg.handlers.clear()
        try:
            setup_logging(file=False, actions=False, **kwargs)
            sh = next(h for h in lg.handlers if isinstance(h, logging.StreamHandler))
            return sh.formatter._formatters[logging.INFO]._fmt
        finally:
            lg.handlers[:] = saved

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
