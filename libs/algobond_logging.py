"""Project log files shared by the command-line tools and the local UI."""
import configparser
import errno
import json
import logging
import os
import stat
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


if os.name == "nt":
    import msvcrt


LOG_FILES = {"errors": "errors.log", "debug": "debug.log", "transactions": "transactions.log"}
TRANSACTIONS = "algobond.transactions"
logger = logging.getLogger(__name__)
active_options = ContextVar("algobond_log_options", default=None)


@dataclass(frozen=True)
class LogOptions:
    enable_logs_errors: bool = True
    enable_logs_debugs: bool = False
    enable_logs_transactions: bool = False

    @classmethod
    def from_ini(cls, config):
        defaults = cls()
        values = {}
        for key, default in vars(defaults).items():
            try:
                value = config.get("settings", key, fallback=None)
            except configparser.Error:
                logger.warning("Не удалось прочитать параметр %s. Используется значение по умолчанию.", key)
                continue
            if value is None:
                continue
            value = value.strip().lower()
            if value in {"true", "yes", "1", "t", "y"}:
                values[key] = True
            elif value in {"false", "no", "0", "f", "n"}:
                values[key] = False
            else:
                logger.warning("Недопустимое значение параметра %s. Используется %s.", key, default)
        return cls(**values)

    @classmethod
    def for_profiles(cls, profiles):
        defaults = cls()
        return cls(**{
            key: any(getattr(profile, key, default) for profile in profiles)
            for key, default in vars(defaults).items()
        }) if profiles else defaults


@contextmanager
def profile_logging(profile):
    token = active_options.set(profile)
    try:
        yield
    finally:
        active_options.reset(token)


@contextmanager
def append_lock(path):
    # POSIX append writes are positioned atomically. Windows Python uses the
    # CRT append mode, which can seek and write separately across processes.
    if os.name != "nt":
        yield
        return
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        lock.seek(0)
        deadline = time.monotonic() + 2
        while True:
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EDEADLK) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            yield
        finally:
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class ProjectLogHandler(logging.Handler):
    def __init__(self, project, options):
        super().__init__()
        self.folder = Path(project).resolve() / "logs"
        self.options = options
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"))
        self.console = logging.StreamHandler()
        self.console.setFormatter(self.formatter)

    def emit(self, record):
        options = active_options.get()
        if options is None:
            options = self.options
        debug = getattr(options, "enable_logs_debugs", False)
        if record.name == TRANSACTIONS:
            if getattr(options, "enable_logs_transactions", False):
                self.append("transactions", record, debug)
            return
        if debug:
            self.console.handle(record)
        if getattr(options, "enable_logs_errors", True) and record.levelno >= logging.WARNING:
            self.append("errors", record, debug)
        if debug:
            self.append("debug", record, debug)

    def append(self, kind, record, debug):
        try:
            self.folder.mkdir(exist_ok=True, parents=True)
            line = self.format(record).encode("utf-8", errors="backslashreplace") + b"\n"
            path = self.folder / LOG_FILES[kind]
            with append_lock(path), path.open("ab", buffering=0) as stream:
                stream.write(line)
        except (OSError, ValueError):
            if debug:
                notice = logging.LogRecord(__name__, logging.WARNING, "", 0,
                                           "Не удалось записать logs/%s.", (LOG_FILES[kind],), None)
                self.console.handle(notice)


def configure_logging(project, options=None, *, startup=False):
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, ProjectLogHandler):
            root.removeHandler(handler)
            handler.close()
    root.addHandler(ProjectLogHandler(project, options if options is not None else LogOptions()))
    root.setLevel(logging.DEBUG)
    if startup:
        sys.excepthook = log_uncaught
        threading.excepthook = log_thread_exception


def log_failure(target, operation, error, *, detail=None, level=logging.WARNING):
    frames = traceback.extract_tb(error.__traceback__)
    location = " → ".join(f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})" for frame in frames)
    message = detail if detail is not None else type(error).__name__
    target.log(level, "%s: %s%s", operation, message, f" [{location}]" if location else "")


def log_uncaught(error_type, error, tb):
    if not issubclass(error_type, Exception):
        sys.__excepthook__(error_type, error, tb)
        return
    log_failure(logger, "Необработанная ошибка программы", error, level=logging.ERROR)


def log_thread_exception(args):
    if issubclass(args.exc_type, Exception):
        log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)
    else:
        threading.__excepthook__(args)


def read_account_config(path):
    path = Path(path)
    if path.suffix.lower() != ".ini" or not path.is_file():
        return None
    config = configparser.ConfigParser()
    try:
        with path.open(encoding="utf-8-sig") as stream:
            config.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as error:
        log_failure(logger, "Пропущен файл конфигурации " + path.name, error)
        return None
    return config if config.has_section("client") else None


def log_transaction(profile, order_type, event, **fields):
    order_logger = logging.getLogger(TRANSACTIONS)
    if not getattr(profile, "enable_logs_transactions", False) or not order_logger.isEnabledFor(logging.INFO):
        return
    record = dict(
        broker=getattr(profile, "broker_name", ""),
        login=getattr(profile, "login", ""),
        type=order_type,
        event=event,
        **fields,
    )
    with profile_logging(profile):
        order_logger.info("%s", json.dumps(record, ensure_ascii=False, default=str))


def read_log_tail(project, kind, max_bytes=256 * 1024):
    folder = Path(project).resolve() / "logs"
    path = folder / LOG_FILES[kind]
    if folder.is_symlink() or path.is_symlink() or path.resolve() != path:
        raise PermissionError("Log path points outside its configured location")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise PermissionError("Log path is not a regular file")
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - max_bytes)
            stream.seek(start)
            data = stream.read(size - start)
    except FileNotFoundError:
        return {"exists": False, "text": "", "truncated": False}
    if start and b"\n" in data:
        data = data.split(b"\n", 1)[1]
    return {"exists": True, "text": data.decode("utf-8", errors="replace"), "truncated": bool(start)}
