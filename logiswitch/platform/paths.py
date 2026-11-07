"""Per-platform locations and logging setup."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import platform
import sys
import threading
from pathlib import Path
from typing import IO

APP_NAME = "logiswitch"
SERVICE_LABEL = "com.abd3lraouf.logiswitch"
#: v1 registered itself under this name; installers remove it on upgrade.
LEGACY_TASK_NAME = "MXSwitch"
#: LaunchAgent labels used before the current one. Install and uninstall boot these
#: out so a renamed label never leaves an old agent running alongside the new one.
LEGACY_SERVICE_LABELS = ("com.appbuildersgang.logiswitch", "com.abd3lraouf.mxswitch")
#: Set in the LaunchAgent plist. Tells the agent its stderr is already being captured,
#: so it should not also echo to the console.
MANAGED_ENV_VAR = "LOGISWITCH_MANAGED"


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def default_target_os() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    return "linux"


def data_dir() -> Path:
    if is_windows():
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LogiSwitch"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / APP_NAME


def log_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    return data_dir() / f"{APP_NAME}.log"


def trace_path() -> Path:
    """Where an anomalous frame trace is dumped.

    Its own file, not :func:`log_path`: a trace is thousands of lines and would
    rotate the surrounding diagnosis straight out of a 512 KiB log.
    """
    return log_path().with_name(f"{APP_NAME}.trace.log")


def crash_path() -> Path:
    """Where a native fault (a crash inside hidapi or a ctypes call) leaves its stacks.

    A segfault kills the process before any logging handler can run, so
    :mod:`faulthandler` writes straight to this file instead.
    """
    return log_path().with_name(f"{APP_NAME}.crash.log")


def doctor_report_path() -> Path:
    """Where ``logiswitch doctor`` leaves its report, ready to attach to a bug."""
    return log_path().with_name(f"{APP_NAME}-doctor.txt")


def launchd_stdio_path() -> Path:
    """Where launchd dumps the agent's raw stdout/stderr.

    Deliberately not :func:`log_path`: the agent logs there itself, and having launchd
    redirect the same stream into the same file writes every line twice. This one holds
    only what escapes the logger -- tracebacks from a failed import, mostly.
    """
    return log_path().with_name(f"{APP_NAME}.launchd.log")


def is_managed() -> bool:
    """True when running as the installed background agent."""
    return os.environ.get(MANAGED_ENV_VAR) == "1"


def state_path() -> Path:
    return data_dir() / "state.json"


def python_executable(windowless: bool = False) -> Path:
    """Interpreter to launch the background agent with.

    On Windows the console-less ``pythonw.exe`` avoids a flashing window at logon.
    """
    executable = Path(sys.executable)
    if windowless and is_windows():
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return executable


def setup_logging(
    verbose: bool = False, log_file: Path | None = None, console: bool = True
) -> None:
    root = logging.getLogger("logiswitch")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Close before dropping: clearing the list alone orphans the rotating handler's
    # open file, leaking a descriptor every time logging is reconfigured.
    for existing in root.handlers[:]:
        # pragma: no cover - a handler that cannot close is not fatal
        with contextlib.suppress(Exception):
            existing.close()
    root.handlers.clear()
    root.propagate = False
    # Milliseconds matter: the races this log exists to diagnose happen inside one
    # second, and without them a log line cannot be ordered against a frame trace.
    # The thread name matters for the same reason: the worker, every HID reader and
    # the OS watcher log concurrently, and each thread is named for what it does.
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # Under pythonw.exe (the Windows logon task) there is no stderr at all; a
    # StreamHandler on None fails, and swallows, every record it is handed.
    if console and sys.stderr is not None:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
        except OSError as exc:  # pragma: no cover - read-only home, etc.
            root.warning("cannot write %s: %s", log_file, exc)

    if not root.handlers:
        root.addHandler(logging.NullHandler())


#: Kept open for the life of the process: faulthandler writes to the descriptor
#: from inside a signal handler, long after any ``with`` block would have closed it.
_fault_file: IO[str] | None = None
#: Crash file beyond this is cut back at start-up, so a crash loop cannot fill a disk.
CRASH_FILE_LIMIT = 256 * 1024


def install_crash_logging(fault_file: Path | None = None, banner: str = "") -> None:
    """Make every way the long-running agent can die leave a trace in its files.

    - An exception escaping any thread is logged with its traceback. The default
      hook prints to stderr, which is nothing at all under pythonw.exe and a
      separate file under launchd, so a dead reader or worker left the log silent
      while the process looked healthy.
    - :mod:`warnings` are routed into the log instead of that same stderr.
    - With ``fault_file``, a native crash writes every thread's Python stack there
      (see :func:`crash_path`); ``banner`` is written first to date the session.
    """
    global _fault_file
    root = logging.getLogger(APP_NAME)

    def log_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        root.critical(
            "uncaught exception in thread %s; that thread has stopped",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),  # type: ignore[arg-type]
        )

    threading.excepthook = log_thread_exception

    logging.captureWarnings(True)
    warnings_log = logging.getLogger("py.warnings")
    warnings_log.handlers[:] = root.handlers
    warnings_log.propagate = False

    if fault_file is None:
        return
    import faulthandler

    try:
        fault_file.parent.mkdir(parents=True, exist_ok=True)
        if fault_file.exists() and fault_file.stat().st_size > CRASH_FILE_LIMIT:
            fault_file.unlink()
        if _fault_file is not None:
            _fault_file.close()
        _fault_file = open(fault_file, "a", encoding="utf-8")  # noqa: SIM115 - see _fault_file
        if banner:
            _fault_file.write(banner + "\n")
            _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
    except OSError as exc:  # pragma: no cover - read-only home, etc.
        root.warning("cannot write %s, native crashes will leave no trace: %s", fault_file, exc)
