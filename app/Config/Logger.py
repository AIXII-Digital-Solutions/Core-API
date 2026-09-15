import atexit
import datetime
import glob
import logging
import multiprocessing
import os
import queue
import zipfile
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from .config import LOGS_DIR, DEV_MODE


class CustomLogHandler(RotatingFileHandler):
    def __init__(self, filename, maxBytes, backupCount):
        self.backup_count = backupCount
        self.log_directory = os.path.dirname(filename)
        self.base_filename = os.path.basename(filename)
        super().__init__(
            filename=filename,
            maxBytes=maxBytes,
            backupCount=0,
            encoding='utf-8',
            delay=False
        )

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"{self.baseFilename}.{timestamp}"
        os.rename(self.baseFilename, archive_name)

        self._archive_file(archive_name)

        self.stream = self._open()

        self._cleanup_logs()

    def _archive_file(self, filename):
        zip_name = f"{filename}.zip"
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(filename, os.path.basename(filename))
        os.remove(filename)

    def _cleanup_logs(self):
        zip_files = glob.glob(os.path.join(self.log_directory, "*.zip"))
        zip_files.sort(key=os.path.getctime, reverse=True)

        while len(zip_files) > self.backup_count:
            os.remove(zip_files.pop())


log_format = (
    '%(levelname)s:     [%(name)s] %(asctime)s | %(filename)s-%(lineno)d: %(message)s'
)

# Noisy third-party loggers pinned to ERROR.
_BLACKLIST = (
    "httpcore.http2", "hpack.hpack", "hpack.table",
    "asyncio", "httpcore.connection", "httpx",
)


def _resolve_level() -> int:
    """Log level from the LOG_LEVEL env var (DEBUG/INFO/WARNING/ERROR/CRITICAL); falls back
    to DEBUG in DEV_MODE else INFO. Lets ops tune verbosity per environment without code edits."""
    name = os.getenv("LOG_LEVEL", "").strip().upper()
    if name:
        level = getattr(logging, name, None)
        if isinstance(level, int):
            return level
    return logging.DEBUG if DEV_MODE else logging.INFO


# All loggers of this process feed ONE queue, drained by ONE listener thread that owns the real handlers.
# A logging call on the event loop then costs a queue put; the console write, the file write and the
# rotating handler's size check happen on the listener thread, never blocking a request.
_log_queue: "queue.SimpleQueue[logging.LogRecord]" = queue.SimpleQueue()
_listener: "QueueListener | None" = None


def _ensure_listener() -> None:
    global _listener
    if _listener is not None:
        return
    # Records arrive already formatted (see _PreformattingQueueHandler) — each logger keeps its own format.
    passthrough = logging.Formatter("%(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(passthrough)
    handlers = [console_handler]

    # Only the MAIN process writes to the rotating file. Child processes — ProcessPoolExecutor workers and
    # uvicorn's worker processes alike — must NOT attach their own RotatingFileHandler to the same file:
    # concurrent doRollover() across processes races/corrupts. They log to the console, which the
    # container captures.
    if multiprocessing.parent_process() is None:
        log_file = LOGS_DIR / f"LOG_{datetime.datetime.now().strftime('%Y-%m-%d')}.log"
        file_handler = CustomLogHandler(
            filename=log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MiB
            backupCount=5
        )
        file_handler.setFormatter(passthrough)
        handlers.append(file_handler)

    _listener = QueueListener(_log_queue, *handlers)
    _listener.start()
    atexit.register(_listener.stop)   # flush what is still queued on a clean exit


class _PreformattingQueueHandler(QueueHandler):
    """QueueHandler.prepare() formats the record with THIS handler's formatter before it is queued, so
    per-logger formats survive a shared listener; tracebacks are folded into the message too."""


def setup_logger(name: str, log_format: str = log_format) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level())
    logger.propagate = False  # don't double-emit via the root logger

    if logger.handlers:
        # Already configured (idempotent): refresh the level but don't add duplicate handlers.
        return logger

    _ensure_listener()
    queue_handler = _PreformattingQueueHandler(_log_queue)
    queue_handler.setFormatter(logging.Formatter(log_format))
    logger.addHandler(queue_handler)

    for module in _BLACKLIST:
        logging.getLogger(module).setLevel(logging.ERROR)

    return logger


__all__ = [
    "setup_logger",
]
