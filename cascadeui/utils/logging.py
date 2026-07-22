# // ========================================( Modules )======================================== // #


import atexit
import json as _json
import logging
import os
import queue
import sys
from dataclasses import dataclass
from datetime import datetime
from logging import (
    CRITICAL,
    DEBUG,
    ERROR,
    INFO,
    WARNING,
    FileHandler,
    Formatter,
    Handler,
    LogRecord,
    StreamHandler,
)
from logging.handlers import QueueHandler, QueueListener
from typing import Optional, Union

# // ========================================( Color Schemes )======================================== // #


@dataclass
class ColorScheme:
    """ANSI color definitions for console log output.

    Each attribute is a raw ANSI escape string. Set any to ``""`` to disable
    coloring for that element.

    Usage::

        # Tweak a single color
        scheme = ColorScheme(info="\\x1b[36m")  # cyan info

        # Or build from scratch
        scheme = ColorScheme(
            debug="\\x1b[90m",
            info="\\x1b[34;1m",
            warning="\\x1b[33;1m",
            error="\\x1b[31m",
            critical="\\x1b[31;1m",
            timestamp="\\x1b[31m",
            function="\\x1b[33;1m",
            name="\\x1b[32;1m",
        )
    """

    # \\ level colors
    debug: str = "\x1b[38;1m"
    info: str = "\x1b[34;1m"
    warning: str = "\x1b[33;1m"
    error: str = "\x1b[31m"
    critical: str = "\x1b[31;1m"
    # \\ element colors
    timestamp: str = "\x1b[31m"
    function: str = "\x1b[33;1m"
    name: str = "\x1b[32;1m"
    # \\ reset sequence
    reset: str = "\x1b[0m"

    def level_color(self, level: int) -> str:
        """Return the color string for a given log level."""
        return {
            DEBUG: self.debug,
            INFO: self.info,
            WARNING: self.warning,
            ERROR: self.error,
            CRITICAL: self.critical,
        }.get(level, self.debug)


# \\ built-in color schemes
COLOR_SCHEMES: dict[str, ColorScheme] = {
    "default": ColorScheme(),
    "ocean": ColorScheme(
        debug="\x1b[37m",
        info="\x1b[36;1m",
        warning="\x1b[33m",
        error="\x1b[35;1m",
        critical="\x1b[31;1m",
        timestamp="\x1b[34m",
        function="\x1b[36m",
        name="\x1b[34;1m",
    ),
    "forest": ColorScheme(
        debug="\x1b[37m",
        info="\x1b[32;1m",
        warning="\x1b[33;1m",
        error="\x1b[31;1m",
        critical="\x1b[31;1m",
        timestamp="\x1b[33m",
        function="\x1b[32m",
        name="\x1b[32;1m",
    ),
    "none": ColorScheme(
        debug="",
        info="",
        warning="",
        error="",
        critical="",
        timestamp="",
        function="",
        name="",
        reset="",
    ),
}


# // ========================================( Format Templates )======================================== // #


@dataclass
class FormatTemplate:
    """Defines the layout of a log line.

    The ``stream_fmt`` string may contain color tokens (``$``-delimited) that
    are replaced at build time:  ``$ts$``, ``$lvl$``, ``$fn$``, ``$name$``,
    ``$r$`` (timestamp, level, function, name, and reset respectively).
    Logging fields use standard ``{``-style placeholders (``{asctime}``, etc.).

    The ``file_fmt`` string is plain (no color tokens) and uses the same
    ``{``-style placeholders.

    Usage::

        template = FormatTemplate(
            stream_fmt="[$ts${asctime}$r$] $lvl${levelname}$r$ {message}",
            file_fmt="[{asctime}] {levelname} {message}",
        )
    """

    stream_fmt: str = (
        "[$ts${asctime}$r$] [$lvl${levelname:<8}$r$] "
        "[$fn${funcName:^21}$r$] $name${name}$r$ {message}"
    )
    file_fmt: str = "[{asctime}] [{levelname:<8}] [{funcName:^21}] [{name}] {message}"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    capitalize_module: bool = True


# \\ built-in format templates
FORMAT_TEMPLATES: dict[str, FormatTemplate] = {
    "default": FormatTemplate(),
    "minimal": FormatTemplate(
        stream_fmt="$lvl${levelname:<8}$r$ $name${name}$r$  {message}",
        file_fmt="[{levelname:<8}] [{name}] {message}",
    ),
    "detailed": FormatTemplate(
        stream_fmt=(
            "[$ts${asctime}$r$] [$lvl${levelname:<8}$r$] "
            "[$fn${funcName:^21}$r$] $name${name}$r$ "
            "({filename}:{lineno}) {message}"
        ),
        file_fmt=(
            "[{asctime}] [{levelname:<8}] [{funcName:^21}] [{name}] "
            "({filename}:{lineno}) {message}"
        ),
    ),
    "compact": FormatTemplate(
        stream_fmt="[$ts${asctime}$r$] $lvl${levelname:<8}$r$ {message}",
        file_fmt="[{asctime}] {levelname:<8} {message}",
    ),
}


# // ========================================( Formatters )======================================== // #


class ColoredStreamFormatter(Formatter):
    """Colored console formatter with per-level ANSI styling.

    Pre-builds a ``Formatter`` for each log level at init time so that
    ``.format()`` never allocates a new ``Formatter`` on the hot path.

    Args:
        colors: A ``ColorScheme`` instance or a preset name from ``COLOR_SCHEMES``.
        template: A ``FormatTemplate`` instance or a preset name from ``FORMAT_TEMPLATES``.
    """

    def __init__(
        self,
        colors: Union[ColorScheme, str, None] = None,
        template: Union[FormatTemplate, str, None] = None,
    ):
        super().__init__()
        # \\ resolve colors
        if colors is None:
            self.colors = COLOR_SCHEMES["default"]
        elif isinstance(colors, str):
            self.colors = COLOR_SCHEMES.get(colors, COLOR_SCHEMES["default"])
        else:
            self.colors = colors

        # \\ resolve template
        if template is None:
            self.template = FORMAT_TEMPLATES["default"]
        elif isinstance(template, str):
            self.template = FORMAT_TEMPLATES.get(template, FORMAT_TEMPLATES["default"])
        else:
            self.template = template

        # \\ pre-build a Formatter for each log level
        self._formatters: dict[int, Formatter] = {}
        for level in (DEBUG, INFO, WARNING, ERROR, CRITICAL):
            fmt = (
                self.template.stream_fmt.replace("$ts$", self.colors.timestamp)
                .replace("$lvl$", self.colors.level_color(level))
                .replace("$fn$", self.colors.function)
                .replace("$name$", self.colors.name)
                .replace("$r$", self.colors.reset)
            )
            self._formatters[level] = Formatter(fmt, self.template.datefmt, style="{")

    def format(self, record: LogRecord) -> str:
        # \\ work on a copy so downstream handlers see the original record
        copy = logging.makeLogRecord(record.__dict__)
        if self.template.capitalize_module:
            copy.module = copy.module.capitalize()
        formatter = self._formatters.get(copy.levelno, self._formatters[DEBUG])
        return formatter.format(copy)


class FileFormatter(Formatter):
    """Plain-text file formatter.

    Args:
        template: A ``FormatTemplate`` instance or a preset name from ``FORMAT_TEMPLATES``.
    """

    def __init__(self, template: Union[FormatTemplate, str, None] = None):
        # \\ resolve template
        if template is None:
            self.template = FORMAT_TEMPLATES["default"]
        elif isinstance(template, str):
            self.template = FORMAT_TEMPLATES.get(template, FORMAT_TEMPLATES["default"])
        else:
            self.template = template

        super().__init__(
            fmt=self.template.file_fmt,
            datefmt=self.template.datefmt,
            style="{",
        )

    def format(self, record: LogRecord) -> str:
        copy = logging.makeLogRecord(record.__dict__)
        if self.template.capitalize_module:
            copy.module = copy.module.capitalize()
        return super().format(copy)


class JSONFormatter(Formatter):
    """Structured JSON formatter for machine-readable log files.

    Note: under ``setup_logging``'s async queue, the ``QueueHandler`` pre-formats
    each record and clears ``exc_info`` before it reaches a formatter, so a
    traceback lands inside the ``message`` field rather than a separate
    ``exception`` key. To get the structured ``exception`` key, attach a
    synchronous ``FileHandler`` carrying this formatter via
    ``setup_logging(handler=...)``.

    Args:
        fields: Log record attributes to include. Defaults to a standard set.
        indent: JSON indent level. ``None`` for single-line output.
    """

    DEFAULT_FIELDS = ("asctime", "levelname", "name", "funcName", "message")

    def __init__(
        self,
        fields: tuple[str, ...] | None = None,
        indent: int | None = None,
    ):
        super().__init__(datefmt="%Y-%m-%dT%H:%M:%S")
        self.fields = fields or self.DEFAULT_FIELDS
        self.indent = indent

    def format(self, record: LogRecord) -> str:
        record.message = record.getMessage()
        if "asctime" in self.fields:
            record.asctime = self.formatTime(record, self.datefmt)
        data = {f: getattr(record, f, None) for f in self.fields}
        if record.exc_info and record.exc_info[1] is not None:
            data["exception"] = self.formatException(record.exc_info)
        return _json.dumps(data, default=str, ensure_ascii=False, indent=self.indent)


# // ========================================( Queue Machinery )======================================== // #


# \\ single queue pipeline owned by setup_logging: log calls enqueue records,
# \\ the listener thread performs the actual console/file I/O
_queue_handler: Optional[QueueHandler] = None
_queue_listener: Optional[QueueListener] = None
_direct_handler: Optional[Handler] = None
_atexit_registered: bool = False


def _stop_queue_listener() -> None:
    """Stop the listener thread, draining every queued record first."""
    global _queue_listener
    if _queue_listener is not None:
        _queue_listener.stop()
        _queue_listener = None


def _teardown_handlers(root_logger: logging.Logger) -> None:
    """Detach and release everything a previous ``setup_logging`` call installed.

    Sink handlers created by the library are closed. A caller-supplied
    ``handler=`` is detached but left open because the caller owns it.
    Handlers the caller attached to the logger directly are never touched.
    """
    global _queue_handler, _direct_handler
    if _queue_listener is not None:
        sinks = _queue_listener.handlers
        _stop_queue_listener()
        for sink in sinks:
            try:
                sink.close()
            except (OSError, ValueError):
                pass
    if _queue_handler is not None:
        root_logger.removeHandler(_queue_handler)
        _queue_handler.close()
        _queue_handler = None
    if _direct_handler is not None:
        root_logger.removeHandler(_direct_handler)
        _direct_handler = None


def _install_async_handlers(root_logger: logging.Logger, sinks: list[Handler]) -> None:
    """Route ``root_logger`` records through a queue drained on a background thread.

    The standard ``QueueHandler`` / ``QueueListener`` pairing: the logger's
    only output handler is a non-blocking queue put, and the listener thread
    owns the stream/file handlers that perform the actual I/O. The listener
    is stopped, with its queue drained, at interpreter exit; the sink
    handlers are then flushed and closed by ``logging.shutdown``.
    """
    global _queue_handler, _queue_listener, _atexit_registered
    log_queue: queue.Queue = queue.Queue(-1)
    _queue_handler = QueueHandler(log_queue)
    _queue_listener = QueueListener(log_queue, *sinks, respect_handler_level=True)
    # \\ same convention as logging.config: the listener rides on its handler
    _queue_handler.listener = _queue_listener
    root_logger.addHandler(_queue_handler)
    _queue_listener.start()
    if not _atexit_registered:
        atexit.register(_stop_queue_listener)
        _atexit_registered = True


# // ========================================( Setup )======================================== // #


_NO_COLOR_SCHEME = ColorScheme(
    debug="",
    info="",
    warning="",
    error="",
    critical="",
    timestamp="",
    function="",
    name="",
    reset="",
)


def _stream_supports_color(stream) -> bool:
    """Whether ``stream`` can render ANSI color.

    Honors ``NO_COLOR`` / ``FORCE_COLOR``, then delegates to discord.py's own
    detector (tty plus Windows-terminal capability), with an ``isatty``
    fallback if that helper is unavailable.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        import discord.utils

        return discord.utils.stream_supports_colour(stream)
    except Exception:
        return bool(getattr(stream, "isatty", lambda: False)())


def setup_logging(
    *,
    level: Union[int, str] = logging.INFO,
    actions: Union[bool, str] = True,
    file: bool = True,
    stream: bool = True,
    trace: bool = False,
    path: str = "logs",
    max_files: int = 10,
    prefix: str = "cascadeui",
    mode: str = "a",
    encoding: str = "utf-8",
    colors: Union[ColorScheme, str, None] = None,
    color: Optional[bool] = None,
    template: Union[FormatTemplate, str, None] = None,
    stream_formatter: Optional[Formatter] = None,
    file_formatter: Optional[Formatter] = None,
    handler: Optional[Handler] = None,
) -> None:
    """Configure logging for the ``cascadeui`` library.

    Attaches handlers to the ``"cascadeui"`` logger so all library modules
    produce output. Without calling this, CascadeUI is silent.

    Log calls are non-blocking: the ``"cascadeui"`` logger receives a single
    ``QueueHandler``, and a background ``QueueListener`` thread owns the
    console and file handlers, so the actual I/O never runs on the calling
    thread. This is the standard queue pattern from the Python logging
    cookbook for handlers that block. The listener drains its queue and stops
    at interpreter exit, so records logged before a normal shutdown are
    flushed to their targets. The listener thread does not survive
    ``os.fork()``; a forked child process must call ``setup_logging()``
    again.

    Calling ``setup_logging`` again reconfigures from scratch: handlers the
    previous call attached are removed first (library-created ones are also
    closed), so repeat calls never double-log. Handlers attached to the
    logger through other means are left in place.

    Quick start::

        from cascadeui import setup_logging
        setup_logging()

    Custom level and color scheme::

        setup_logging(level="WARNING", colors="ocean")

    Disable file output::

        setup_logging(file=False)

    Enable ViewStore dispatch tracing::

        setup_logging(level="DEBUG", trace=True)

    Bring your own handler::

        setup_logging(handler=my_handler, level="DEBUG")

    Standard Python control still works after setup::

        logging.getLogger("cascadeui").setLevel(logging.WARNING)
        logging.getLogger("cascadeui.state.store").setLevel(logging.DEBUG)

    Action dispatch logging is installed automatically (``actions=True`` by
    default, emitting at ``INFO``). Set ``actions=False`` to drop the action
    stream entirely, or pass a level string to place it lower so it stays out
    of INFO logs and surfaces only when that level is enabled::

        setup_logging(level="INFO", actions=False)       # no action stream
        setup_logging(level="DEBUG", actions="DEBUG")    # action stream only at DEBUG

    Args:
        level:             Log level for the ``"cascadeui"`` logger.
        actions:           Auto-install ``LoggingMiddleware`` so every
                           dispatched action is logged to
                           ``"cascadeui.actions"``. ``True`` (default)
                           installs it at ``INFO``; pass a level string
                           (e.g. ``"DEBUG"``) to set the action stream's
                           emission level so it hides under INFO logging and
                           surfaces only at that level; ``False`` skips it.
        file:              Whether to write to a date-stamped log file.
        stream:            Whether to write colored output to the console.
        trace:             Install the ViewStore dispatch-miss tracer. Wraps
                           discord.py's ``ViewStore.dispatch_view`` to log
                           the full dispatch table when an interaction
                           targets a stale item. Intended for debugging
                           "View interaction referencing unknown view" errors.
        path:              Directory for log files.
        max_files:         Maximum log files before old ones are purged.
        prefix:            Filename prefix (e.g. ``"cascadeui"`` produces
                           ``cascadeui-2026-04-16.log``).
        mode:              File open mode (``"a"`` = append, ``"w"`` = overwrite).
        encoding:          Log file encoding.
        colors:            ``ColorScheme`` instance or preset name.
        color:             Force console color on (``True``) or off (``False``).
                           Default ``None`` auto-detects: color is emitted only
                           when the stream supports ANSI (a tty with terminal
                           color capability), and never when ``NO_COLOR`` is set,
                           so a raw Windows console shows plain text instead of
                           literal escape codes.
        template:          ``FormatTemplate`` instance or preset name.
        stream_formatter:  Custom ``Formatter`` for console output.
        file_formatter:    Custom ``Formatter`` for file output.
        handler:           A pre-built handler to attach directly. When provided,
                           ``file``, ``stream``, ``colors``, ``color``,
                           ``template``, ``stream_formatter``, and
                           ``file_formatter`` are ignored. The handler runs
                           synchronously on the calling thread (no queue)
                           because the caller owns its threading behavior;
                           pass a ``logging.handlers.QueueHandler`` for
                           off-thread handling.
    """
    global _direct_handler

    root_logger = logging.getLogger("cascadeui")

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root_logger.setLevel(level)

    # \\ reconfigure: release whatever a previous call installed
    _teardown_handlers(root_logger)

    if handler is not None:
        # Bring-your-own-handler: attached synchronously, sink setup skipped.
        # trace and the action stream below still install, so a custom handler
        # also receives the ViewStore tracer and the dispatched-action logs.
        root_logger.addHandler(handler)
        _direct_handler = handler
    else:
        sinks: list[Handler] = []

        if stream:
            if stream_formatter is not None:
                resolved_stream_fmt = stream_formatter
            else:
                # Emit color only when the console can render it: an explicit
                # ``color=`` wins, otherwise detect. A no-color scheme keeps the
                # same layout so a raw Windows console shows plain text instead of
                # literal ``^[[31m`` escapes, matching the plain file handler.
                use_color = color if color is not None else _stream_supports_color(sys.stdout)
                resolved_stream_fmt = ColoredStreamFormatter(
                    colors=colors if use_color else _NO_COLOR_SCHEME, template=template
                )
            sh = StreamHandler(sys.stdout)
            sh.setFormatter(resolved_stream_fmt)
            sinks.append(sh)

        if file:
            resolved_file_fmt = file_formatter or FileFormatter(template=template)
            date = str(datetime.now().date())
            filename = f"{prefix}-{date}.log" if prefix else f"{date}.log"
            os.makedirs(path, exist_ok=True)
            fh = FileHandler(filename=f"{path}/{filename}", encoding=encoding, mode=mode)
            fh.setFormatter(resolved_file_fmt)
            sinks.append(fh)

            if max_files > 0:
                _purge_old_log_files(path, prefix, max_files)

        if sinks:
            _install_async_handlers(root_logger, sinks)

    if trace:
        from ..tracing import _install_viewstore_trace

        _install_viewstore_trace()

    if actions:
        from ..state.middleware import LoggingMiddleware
        from ..state.singleton import get_store

        store = get_store()
        if not store.has_middleware(LoggingMiddleware):
            action_level = actions if isinstance(actions, str) else "INFO"
            store._add_middleware(LoggingMiddleware(level=action_level))


def _purge_old_log_files(log_dir: str, prefix: Optional[str], max_files: int) -> None:
    """Remove oldest log files when count exceeds max_files."""
    try:
        all_files = os.listdir(log_dir)
    except OSError:
        return

    if prefix:
        matching = [f for f in all_files if f.startswith(prefix) and f.endswith(".log")]
    else:
        matching = [f for f in all_files if f.endswith(".log")]

    if len(matching) <= max_files:
        return

    matching_paths = [os.path.join(log_dir, f) for f in matching]
    matching_paths.sort(key=os.path.getctime)
    for filepath in matching_paths[:-max_files]:
        try:
            os.unlink(filepath)
        except (PermissionError, OSError):
            pass
