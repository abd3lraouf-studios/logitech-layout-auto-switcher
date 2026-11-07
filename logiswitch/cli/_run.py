"""Foreground-run commands: ``watch`` (the agent) and ``notify-test``."""

from __future__ import annotations

import argparse
import contextlib
import logging
import platform
import signal
import sys
from datetime import datetime

from .. import __version__, notify
from ..agent import Agent, AgentConfig
from ..hidpp import backend
from ..hidpp import protocol as p
from ..platform import crash_path, default_target_os
from ..platform.paths import install_crash_logging

log = logging.getLogger("logiswitch")


def cmd_watch(args: argparse.Namespace) -> int:
    # Resolved through the cli package at call time so tests that monkeypatch
    # ``cli.state_path`` reach this call.
    from . import state_path

    config = AgentConfig(
        target_os=p.normalise_os(args.os or default_target_os()),
        reassert_interval=args.reassert,
        force_polling=args.polling,
        state_file=state_path(),
        notify=args.notify,
        observe=args.observe,
        active_window=args.active_window,
        claim_host=args.claim_host,
        event_only=args.event_only,
        event_only_reassert=args.event_only_reassert,
    )
    agent = Agent(config)

    if args.once:
        return 0 if agent.assert_once() else 1

    # The first thing a bug report's log should answer: what exactly was running.
    environment = (
        f"logiswitch {__version__} on {platform.platform()}, "
        f"python {platform.python_version()} ({sys.executable}), hid {backend.describe()}"
    )
    log.info("%s", environment)
    log.info(
        "options: verbose=%s debounce=%.1fs active-window=%.0fs event-only-reassert=%s "
        "polling=%s notify=%s",
        bool(args.verbose or args.trace),
        config.debounce,
        config.active_window,
        f"{config.event_only_reassert:.0f}s" if config.event_only_reassert else "off",
        bool(args.polling),
        bool(args.notify),
    )
    if sys.platform == "darwin" and not backend.NON_EXCLUSIVE:
        log.warning(
            "could not make HID opens non-exclusive with this hidapi build; while the "
            "agent runs, keys Logi Options+ has diverted (e.g. the calculator key) may "
            "do nothing"
        )
    install_crash_logging(
        crash_path(),
        banner=f"=== {datetime.now().isoformat(timespec='seconds')} {environment}",
    )

    def handle_signal(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.info("received %s, shutting down", name)
        agent.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handle_signal)

    agent.run_forever()
    return 0


def cmd_notify_test(_args: argparse.Namespace) -> int:
    """Prove a notification can actually reach the desktop.

    Worth its own command because the failure is silent: on macOS a notification the
    user has not allowed simply does not appear, and nothing errors. Waiting for a
    real layout change to discover this is a poor way to find out.

    On macOS this is also where the notifier app gets built and, the first time,
    where macOS asks whether to allow its notifications at all.
    """
    notifier = notify.Notifier()
    print(f"backend: {notify.backend_name()}")
    if not notifier.enabled:
        print("no notification backend on this platform", file=sys.stderr)
        return 1
    note = notify.Notification(
        "test", "If you can see this, notifications are working.", notify.APP_TITLE
    )
    if notifier.deliver(note):
        print("sent -- if no notification appeared, it is being blocked:")
        print(f"  macOS:   System Settings > Notifications > {notify.macos_settings_name()}")
        print("  Windows: Settings > System > Notifications")
        return 0
    print("the notification command failed; re-run with -v for the reason", file=sys.stderr)
    return 1
