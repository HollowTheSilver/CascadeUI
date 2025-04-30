
# // ========================================( Modules )======================================== // #

import os
import sys
from datetime import datetime
from logging import (
    Logger,
    Formatter,
    FileHandler,
    StreamHandler,
    LogRecord,
    Handler,
    DEBUG,
    INFO,
    WARNING,
    ERROR,
    CRITICAL,
)
from typing import (
    Any,
    Optional,

)


# // ========================================( Logger )======================================== // #


class ColoredStreamFormatter(Formatter):
    # \\ colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # \\ styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        DEBUG: gray + bold,
        INFO: blue + bold,
        WARNING: yellow + bold,
        ERROR: red,
        CRITICAL: red + bold,
    }

    def format(self, record: LogRecord) -> str:
        record.module = record.module.capitalize()
        log_color: str = self.COLORS[record.levelno]
        format = ( # NOQA
            "[(red){asctime}(reset)] [(level_color){levelname:<8}(reset)] "
            "[(yellow){funcName:^21}(reset)] (green){name}(reset) {message}")
        format: str = format.replace("(reset)", self.reset) # NOQA
        format: str = format.replace("(level_color)", log_color) # NOQA
        format: str = format.replace("(red)", self.red)  # NOQA
        format: str = format.replace("(yellow)", self.yellow + self.bold)  # NOQA
        format: str = format.replace("(green)", self.green + self.bold)  # NOQA
        formatter: Formatter = Formatter(format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


class FileFormatter(Formatter):

    def format(self, record: LogRecord) -> str:
        record.module = record.module.capitalize()
        format = ("[{asctime}] [{levelname:<8}] [{funcName:^21}] {name}   {message}")  # NOQA
        formatter: Formatter = Formatter(format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


class AsyncLogger(Logger):

    max_files: int = 20

    def __init__(self, name: str, level: str = 'DEBUG', path: str = 'logs', output: Any = sys.stdout,
                 encoding: str = "utf-8", file: bool = True, stream: bool = True, max_files: int = 10,
                 propagate: bool = False, mode: str = "a", prefix: str = None,):
        super().__init__(name=name, level=level)
        # \\ set properties
        if self.disabled:
            self.disabled: bool = False
        self.propagate: bool = propagate
        self.log_dir: str = path
        self.file_name: str = str(datetime.now().date())
        self.file: bool = file
        self.stream: bool = stream
        self.mode: str = mode
        self.prefix: Optional[str] = prefix
        # \\ set handlers
        self.file_formatter: Formatter = FileFormatter()
        self.stream_formatter: Formatter = ColoredStreamFormatter()
        if self.stream:
            self.stream_handler: StreamHandler = StreamHandler(output)
            self.stream_handler.setFormatter(self.stream_formatter)
            self.addHandler(hdlr=self.stream_handler)
        if self.file:
            # \\ todo: requires a scheduled callback / task to update filename on date change.
            self.file_handler: FileHandler = FileHandler(
                filename=rf'{self.log_dir}/{self.file_name}.log', encoding=encoding, mode=mode)
            self.file_handler.setFormatter(fmt=self.file_formatter)
            self.addHandler(hdlr=self.file_handler)

    async def purge_logs(self) -> None:
        """
            Purge outdated log files from log directory.
        """
        self.info("Gathering log files...")
        join = os.path.join
        unlink = os.unlink
        # \\ log file list comprehension
        log_files = [join(self.log_dir, log) for log in os.listdir(self.log_dir)]
        if len(log_files) < self.__class__.max_files:
            self.info(f"Purge cancelled. Only <{len(log_files)}> logs found.")
        else:
            self.info(f"Purge triggered. <{len(log_files)}> logs found. Purging log files...")
            # \\ get creation time of each file
            create_times: list = [{'fn': log, 'ctime': os.path.getctime(log)} for log in log_files]
            # \\ sort by date
            create_times: list = sorted(create_times, key=lambda x: x['ctime'])
            # \\ get the oldest (keep last max files)
            create_times: list = create_times[:-self.max_files] if self.max_files else create_times
            self.info("Purged <%d> outdated log files." % len(create_times))
            # \\ unlink every file in the creation times list
            for filename in create_times:
                try:
                    unlink(filename['fn'])
                except PermissionError as error:
                    self.info('Skipped file {0}, {1}'.format(filename['fn'], error))

    async def disable(self):
        self.propagate: bool = False
        self.disabled: bool = True
        self.filters.clear()
        handlers: list[Optional[Handler]] = self.handlers.copy()
        for handler in handlers:
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
        current_date: str = str(datetime.now().date())
        if current_date != self.file_name:
            self.file_name: str = current_date
            self.file_handler.baseFilename = f'{self.log_dir}/{current_date}.log'

