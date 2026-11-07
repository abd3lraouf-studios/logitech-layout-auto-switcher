"""Every way the unattended agent can fail must leave something in its log.

The installed agent runs under pythonw.exe (no stderr at all) or launchd (stderr to
a separate file), so a traceback that only reaches stderr is, for a bug report, no
traceback at all.
"""

from __future__ import annotations

import faulthandler
import logging
import threading
import warnings

import fakehid
import pytest

from logiswitch import cli
from logiswitch.agent import Agent, AgentConfig
from logiswitch.hidpp import protocol as p
from logiswitch.platform import paths
from logiswitch.platform.watchers import polling


@pytest.fixture
def crash_hooks(monkeypatch):
    """Let a test install the process-wide hooks, and take them all back after."""
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    warnings_log = logging.getLogger("py.warnings")
    saved = warnings_log.handlers[:], warnings_log.propagate
    try:
        yield
    finally:
        logging.captureWarnings(False)
        warnings_log.handlers[:], warnings_log.propagate = saved
        if paths._fault_file is not None:
            faulthandler.disable()
            paths._fault_file.close()
            paths._fault_file = None


def _flush():
    for handler in logging.getLogger("logiswitch").handlers:
        handler.flush()


def test_a_crashing_thread_is_logged_with_its_traceback(tmp_path, crash_hooks):
    log_file = tmp_path / "logiswitch.log"
    paths.setup_logging(log_file=log_file, console=False)
    paths.install_crash_logging()

    def boom():
        raise RuntimeError("reader blew up")

    thread = threading.Thread(target=boom, name="hidpp-reader-test")
    thread.start()
    thread.join()
    _flush()

    text = log_file.read_text("utf-8")
    assert "uncaught exception in thread hidpp-reader-test" in text
    assert "RuntimeError: reader blew up" in text
    assert "Traceback" in text


def test_warnings_reach_the_log(tmp_path, crash_hooks):
    log_file = tmp_path / "logiswitch.log"
    paths.setup_logging(log_file=log_file, console=False)
    paths.install_crash_logging()

    # pytest records warnings itself; step outside that to see where they go.
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        logging.captureWarnings(True)
        warnings.warn("something is deprecated", DeprecationWarning, stacklevel=1)
    _flush()

    assert "something is deprecated" in log_file.read_text("utf-8")


def test_the_crash_file_is_dated_and_capped(tmp_path, crash_hooks):
    fault_file = tmp_path / "logiswitch.crash.log"
    fault_file.write_text("x" * (paths.CRASH_FILE_LIMIT + 1))

    paths.install_crash_logging(fault_file, banner="=== session one")

    assert faulthandler.is_enabled()
    # Over the cap, so the old contents were dropped before the new banner.
    assert fault_file.read_text("utf-8") == "=== session one\n"


def test_the_log_line_names_its_thread(tmp_path):
    log_file = tmp_path / "logiswitch.log"
    paths.setup_logging(log_file=log_file, console=False)

    def speak():
        logging.getLogger("logiswitch.test").info("hello")

    thread = threading.Thread(target=speak, name="logiswitch-worker")
    thread.start()
    thread.join()
    _flush()

    assert "[logiswitch-worker] hello" in log_file.read_text("utf-8")


def test_no_console_handler_when_there_is_no_stderr(tmp_path, monkeypatch):
    """pythonw.exe: sys.stderr is None, and a handler on it fails every record."""
    monkeypatch.setattr("sys.stderr", None)
    paths.setup_logging(log_file=tmp_path / "l.log", console=True)

    handlers = logging.getLogger("logiswitch").handlers
    assert not [h for h in handlers if type(h) is logging.StreamHandler]


def test_the_worker_restarts_after_an_unexpected_failure(tmp_path, monkeypatch, caplog):
    from logiswitch import agent as agent_module

    monkeypatch.setattr(agent_module, "WORKER_RESTART_DELAY", 0.0)
    agent = Agent(AgentConfig(target_os="windows", state_file=tmp_path / "state.json"))
    calls = []

    def flaky_loop():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug in the loop")

    monkeypatch.setattr(agent, "_loop", flaky_loop)
    with caplog.at_level(logging.ERROR, logger="logiswitch"):
        agent._run()

    assert len(calls) == 2, "the worker should have been started again"
    assert "agent worker failed unexpectedly" in caplog.text
    assert "bug in the loop" in caplog.text


def test_a_frame_that_breaks_dispatch_does_not_kill_the_reader(transport, monkeypatch, caplog):
    real_dispatch = transport._dispatch
    broken = threading.Event()

    def dispatch_once_broken(data):
        if not broken.is_set():
            broken.set()
            raise ValueError("malformed frame")
        real_dispatch(data)

    monkeypatch.setattr(transport, "_dispatch", dispatch_once_broken)
    with caplog.at_level(logging.ERROR, logger="logiswitch"):
        # The first reply is lost to the broken dispatch and times out ...
        with pytest.raises(p.HidppTimeout):
            transport.request(
                fakehid.MX_KEYS_INDEX,
                p.FEATURE_ROOT,
                p.ROOT_GET_PROTOCOL_VERSION,
                b"\x00\x00\xaa",
                timeout=0.3,
            )
        # ... but the reader is still there for the next one.
        reply = transport.request(
            fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xbb"
        )

    assert reply[:3] == bytes([4, 5, 0xBB])
    assert transport.alive
    assert "could not handle frame" in caplog.text


def test_polling_keeps_its_picture_when_enumeration_fails(monkeypatch, caplog):
    watcher = polling.PollingWatcher(0x046D)
    watcher._present = {("receiver",)}

    def fail(_vendor_id):
        raise OSError("enumeration is broken")

    monkeypatch.setattr(polling.discovery, "find_groups", fail)
    with caplog.at_level(logging.WARNING, logger="logiswitch"):
        first = watcher._snapshot()
        second = watcher._snapshot()

    # Unchanged, so no fake removal and re-arrival.
    assert first == second == {("receiver",)}
    # Warned once, not on every tick.
    assert caplog.text.count("device enumeration failed") == 1


def test_a_fatal_error_reaches_the_log_file_without_verbose(tmp_path, monkeypatch, capsys):
    def explode(_args):
        raise RuntimeError("could not start")

    monkeypatch.setattr(cli, "cmd_status", explode)
    log_file = tmp_path / "logiswitch.log"

    code = cli.main(["status", "--log-file", str(log_file)])
    _flush()

    assert code == 1
    assert "error: could not start" in capsys.readouterr().err
    text = log_file.read_text("utf-8")
    assert "unhandled error, exiting" in text
    assert "RuntimeError: could not start" in text
