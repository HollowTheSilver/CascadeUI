# // ========================================( Modules )======================================== // #


import asyncio
import atexit
import json as _json
import logging
import os
import queue
import sys
from dataclasses import dataclass, field
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
    Logger,
    LogRecord,
    StreamHandler,
)
from logging.handlers import QueueHandler, QueueListener
from typing import Any, Optional, Union

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
    file_fmt: str = "[{asctime}] [{levelname:<8}] [{funcName:^21}] {name}   {message}"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    capitalize_module: bool = True


# \\ built-in format templates
FORMAT_TEMPLATES: dict[str, FormatTemplate] = {
    "default": FormatTemplate(),
    "minimal": FormatTemplate(
        stream_fmt="$lvl${levelname:<8}$r$ $name${name}$r$  {message}",
        file_fmt="{levelname:<8} {name}  {message}",
    ),
    "detailed": FormatTemplate(
        stream_fmt=(
            "[$ts${asctime}$r$] [$lvl${levelname:<8}$r$] "
            "[$fn${funcName:^21}$r$] $name${name}$r$ "
            "({filename}:{lineno}) {message}"
        ),
        file_fmt=(
            "[{asctime}] [{levelname:<8}] [{funcName:^21}] {name}   "
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


# // ========================================( Logger )======================================== // #


class AsyncLogger(Logger):
    """Truly asynchronous logger with colored console output and file rotation.

    Log calls (``.info()``, ``.debug()``, etc.) are non-blocking: records are
    placed onto an in-process queue and written to disk by a background thread
    via ``QueueHandler`` / ``QueueListener``.

    Singleton per name: multiple ``AsyncLogger("foo")`` calls return the same
    instance so handlers are never duplicated and every reference shares state.

    Three levels of customization:

    **Quick** — pick a preset by name::

        logger = AsyncLogger("myapp", colors="ocean", template="minimal")

    **Medium** — pass custom scheme / template objects::

        logger = AsyncLogger("myapp", colors=ColorScheme(info="\\x1b[36;1m"))

    **Full** — supply your own ``Formatter`` instances::

        logger = AsyncLogger("myapp", stream_formatter=MyFormatter())

    Args:
        name:              Logger name (typically ``__name__``).
        level:             Minimum log level (``"DEBUG"``, ``"INFO"``, etc.).
        path:              Directory for log files.
        output:            Stream target for console output.
        encoding:          Log file encoding.
        file:              Whether to write to a log file.
        stream:            Whether to write to the console.
        max_files:         Maximum log files to keep before purging.
        propagate:         Whether to propagate to parent loggers.
        mode:              File open mode (``"a"`` = append, ``"w"`` = overwrite).
        prefix:            Filename prefix — e.g. ``"cascadeui"`` → ``cascadeui-2026-03-16.log``.
        colors:            ``ColorScheme`` instance or preset name (``"default"``,
                           ``"ocean"``, ``"forest"``, ``"none"``).
        template:          ``FormatTemplate`` instance or preset name (``"default"``,
                           ``"minimal"``, ``"detailed"``, ``"compact"``).
        stream_formatter:  Custom ``Formatter`` for console output. Overrides
                           ``colors`` and ``template`` for the stream handler.
        file_formatter:    Custom ``Formatter`` for file output. Overrides
                           ``template`` for the file handler.
    """

    # \\ singleton registry — same name always returns the same instance
    _instances: dict[str, "AsyncLogger"] = {}

    # \\ shared queue — all loggers push here, one listener drains it
    _queue: queue.Queue | None = None
    _listener: QueueListener | None = None

    # \\ deduplicated output handlers keyed by identity
    # (path, prefix, encoding, mode) → FileHandler
    # id(output_stream) → StreamHandler
    _file_handlers: dict[tuple, FileHandler] = {}
    _stream_handlers: dict[int, StreamHandler] = {}
    _atexit_registered: bool = False

    def __new__(cls, name: str, **kwargs) -> "AsyncLogger":
        if name in cls._instances:
            return cls._instances[name]
        instance = super().__new__(cls)
        instance._initialized = False
        cls._instances[name] = instance
        return instance

    def __init__(
        self,
        name: str,
        level: str = "DEBUG",
        path: str = "logs",
        output: Any = sys.stdout,
        encoding: str = "utf-8",
        file: bool = True,
        stream: bool = True,
        max_files: int = 10,
        propagate: bool = False,
        mode: str = "a",
        prefix: Optional[str] = None,
        colors: Union[ColorScheme, str, None] = None,
        template: Union[FormatTemplate, str, None] = None,
        stream_formatter: Optional[Formatter] = None,
        file_formatter: Optional[Formatter] = None,
    ):
        if self._initialized:
            return
        super().__init__(name=name, level=level)

        # \\ set properties
        if self.disabled:
            self.disabled = False
        self.propagate: bool = propagate
        self.log_dir: str = path
        self.max_files: int = max_files
        self.mode: str = mode
        self.prefix: Optional[str] = prefix
        self.file_name: str = self._build_filename()

        # \\ resolve formatters (custom > presets > defaults)
        resolved_stream_fmt = stream_formatter or ColoredStreamFormatter(
            colors=colors, template=template
        )
        resolved_file_fmt = file_formatter or FileFormatter(template=template)

        # \\ initialize the shared queue on first logger
        if AsyncLogger._queue is None:
            AsyncLogger._queue = queue.Queue(-1)

        # \\ get or create shared output handlers (run on listener thread)
        handlers_changed = False

        if stream:
            stream_key = id(output)
            if stream_key not in AsyncLogger._stream_handlers:
                sh = StreamHandler(output)
                sh.setFormatter(resolved_stream_fmt)
                AsyncLogger._stream_handlers[stream_key] = sh
                handlers_changed = True

        if file:
            os.makedirs(self.log_dir, exist_ok=True)
            file_key = (self.log_dir, self.prefix, encoding, self.mode)
            if file_key not in AsyncLogger._file_handlers:
                fh = FileHandler(
                    filename=f"{self.log_dir}/{self.file_name}", encoding=encoding, mode=mode
                )
                fh.setFormatter(resolved_file_fmt)
                AsyncLogger._file_handlers[file_key] = fh
                handlers_changed = True
            self._file_handler = AsyncLogger._file_handlers[file_key]

        # \\ attach a QueueHandler to THIS logger (non-blocking put)
        self.addHandler(QueueHandler(AsyncLogger._queue))

        # \\ start or restart the listener if new output handlers were added
        if handlers_changed:
            AsyncLogger._start_listener()

        # \\ register atexit once to ensure clean shutdown
        if not AsyncLogger._atexit_registered:
            atexit.register(AsyncLogger.shutdown)
            AsyncLogger._atexit_registered = True

        # \\ one-time log cleanup on init — only for this prefix
        if file and self.max_files > 0:
            self._purge_old_logs()

        self._initialized = True

    # // ========================================( Queue Listener )======================================== // #

    @classmethod
    def _start_listener(cls) -> None:
        """Start or restart the background QueueListener with current handlers."""
        if cls._listener is not None:
            cls._listener.stop()
        all_handlers = list(cls._stream_handlers.values()) + list(cls._file_handlers.values())
        cls._listener = QueueListener(cls._queue, *all_handlers, respect_handler_level=True)
        cls._listener.start()

    @classmethod
    def shutdown(cls) -> None:
        """Stop the background listener. Call during application teardown."""
        if cls._listener is not None:
            cls._listener.stop()
            cls._listener = None

    # // ========================================( Helpers )======================================== // #

    def _build_filename(self) -> str:
        """Build the log filename from prefix and current date."""
        date = str(datetime.now().date())
        if self.prefix:
            return f"{self.prefix}-{date}.log"
        return f"{date}.log"

    # // ========================================( Methods )======================================== // #

    def _purge_old_logs(self) -> None:
        """Remove oldest log files when count exceeds max_files.

        Only targets files matching this logger's prefix pattern (e.g.
        ``cascadeui-*.log``), so other log files in the same directory
        are never touched. Called once during ``__init__`` — no background
        tasks or event loop required.
        """
        try:
            all_files = os.listdir(self.log_dir)
        except OSError:
            return

        # Only target files matching this logger's prefix
        if self.prefix:
            matching = [f for f in all_files if f.startswith(self.prefix) and f.endswith(".log")]
        else:
            matching = [f for f in all_files if f.endswith(".log")]

        if len(matching) <= self.max_files:
            return

        # Sort by creation time, remove oldest
        matching_paths = [os.path.join(self.log_dir, f) for f in matching]
        matching_paths.sort(key=os.path.getctime)
        to_remove = matching_paths[: -self.max_files]
        for filepath in to_remove:
            try:
                os.unlink(filepath)
            except (PermissionError, OSError):
                pass

    async def purge_logs(self) -> None:
        """Async version of log cleanup for manual use.

        Runs ``_purge_old_logs`` in a thread. Useful if you want to trigger
        cleanup explicitly at runtime rather than relying on the automatic
        init-time purge.
        """
        await asyncio.to_thread(self._purge_old_logs)

    async def disable(self) -> None:
        """Flush, close, and remove all handlers."""
        self.propagate = False
        self.disabled = True
        self.filters.clear()
        for handler in self.handlers.copy():
            try:
                handler.acquire()
                handler.flush()
                handler.close()
            except (OSError, ValueError):
                pass
            finally:
                handler.release()
            self.removeHandler(handler)

    async def refresh_filename(self) -> None:
        """Roll over to a new date-stamped file if the date has changed."""
        new_name = self._build_filename()
        if new_name != self.file_name:
            self.file_name = new_name
            if hasattr(self, "_file_handler"):
                self._file_handler.baseFilename = f"{self.log_dir}/{new_name}"
