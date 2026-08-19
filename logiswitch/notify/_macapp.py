"""The app bundle macOS attributes this agent's notifications to.

A macOS notification wears the icon and the name of the *process that posted it*,
and no argument to ``display notification`` can override that. ``osascript`` has no
bundle of its own, so everything this agent showed arrived as **Script Editor**: the
wrong icon, the wrong name in Notification Centre, and a single on/off switch in
System Settings shared with every other script on the machine -- silence ours and you
silence them too, and the reverse.

The only way to fix that is to post from a bundle of our own. There is no such bundle
to ship: the agent is a pip-installed Python package with no ``.app`` anywhere. So one
is built at first use, in :func:`~logiswitch.platform.data_dir`, out of tools every Mac
already has -- ``osacompile`` for the applet, ``sips`` and ``iconutil`` for the icon --
and the notification is posted by running it.

Two properties of the ``osascript`` path are kept.

**The text still cannot be quoted wrong.** Body and title travel in the environment,
which the applet reads back verbatim through ``system attribute``. There is no command
line for a device called ``He said "hi"`` to escape into, and nothing is interpolated
into the script -- the script is a constant, compiled once.

**Nothing here can break the agent.** Every step is allowed to fail: a missing tool, a
read-only home, a signature macOS declines. Failure returns None and the caller falls
back to plain ``osascript``, which is exactly where we were before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..platform import data_dir

log = logging.getLogger(__name__)

#: What the notification is attributed to. Shown in Notification Centre and as the
#: entry in System Settings > Notifications the user allows or silences.
DISPLAY_NAME = "Layout Auto Switcher"
BUNDLE_NAME = f"{DISPLAY_NAME}.app"
#: Distinct from the LaunchAgent's ``com.abd3lraouf.logiswitch``: this identifies an
#: app, and giving two different things one identifier confuses LaunchServices.
BUNDLE_ID = "com.abd3lraouf.logiswitch.notifier"

#: How the text reaches the applet.
BODY_ENV = "LOGISWITCH_NOTIFY_BODY"
TITLE_ENV = "LOGISWITCH_NOTIFY_TITLE"

#: Shipped inside the package, so the wheel carries its own icon.
ICON = Path(__file__).with_name("icon.png")
#: Icon sizes to render. 256@2x is 512, which is as large as a notification, the
#: Notification Centre list or the System Settings row will ever draw it; going up to
#: 512@2x triples the size of the .icns for pixels nothing asks for.
ICON_SIZES = (16, 32, 128, 256)

#: Records which script and icon the bundle on disk was built from, so an upgrade
#: that changes either rebuilds it and one that changes neither does not.
STAMP_FILE = "logiswitch-stamp"

#: Building runs four short tools; a minute is generous and still bounded.
BUILD_TIMEOUT = 60.0

_LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks"
    "/LaunchServices.framework/Support/lsregister"
)

#: The whole program. A constant: the text it shows is read from the environment at
#: run time, never compiled into the script.
SOURCE = f"""\
on readEnv(theName)
	try
		return system attribute theName
	on error
		return ""
	end try
end readEnv

on run
	set noteBody to my readEnv("{BODY_ENV}")
	set noteTitle to my readEnv("{TITLE_ENV}")
	if noteTitle is "" then set noteTitle to "logiswitch"
	if noteBody is not "" then
		display notification noteBody with title noteTitle
	end if
end run
"""


def applet_path(bundle: Path) -> Path:
    """The executable inside `bundle`. Running it *is* posting the notification."""
    return bundle / "Contents" / "MacOS" / "applet"


def bundle_path() -> Path:
    return data_dir() / BUNDLE_NAME


def stamp() -> str:
    """Identifies the inputs a built bundle came from.

    Content, not version: a bundle stays valid across an upgrade that did not touch
    the script or the icon, and is rebuilt by one that did -- including a user who
    replaced the icon in their own checkout.
    """
    digest = hashlib.sha256(SOURCE.encode("utf-8"))
    digest.update(BUNDLE_ID.encode("utf-8"))
    digest.update(DISPLAY_NAME.encode("utf-8"))
    try:
        digest.update(ICON.read_bytes())
    except OSError:  # pragma: no cover - the icon ships with the package
        digest.update(b"no icon")
    return digest.hexdigest()


# -- resolving ----------------------------------------------------------------

#: Resolved once per process: building stats several files and hashes the icon, and
#: notifications are throttled to minutes apart, so re-deciding each time is waste.
_resolved: Path | None = None
_tried = False


def ensure() -> Path | None:
    """The applet to run, building the bundle if it is missing or out of date.

    None when this Mac will not give us one; the caller falls back to ``osascript``.
    """
    global _tried, _resolved
    if not _tried:
        _tried = True
        _resolved = _prepare()
    return _resolved


def forget() -> None:
    """Decide again on the next notification.

    Called when running the applet failed -- the bundle was deleted, or an OS upgrade
    invalidated its signature -- so the next notification rebuilds rather than
    retrying something that is now broken.
    """
    global _tried, _resolved
    _tried = False
    _resolved = None


def _prepare() -> Path | None:
    bundle = bundle_path()
    applet = applet_path(bundle)
    wanted = stamp()
    try:
        current = (bundle / "Contents" / "Resources" / STAMP_FILE).read_text("utf-8")
        if current == wanted and applet.exists():
            return applet
    except OSError:
        pass  # not built yet, or half-built
    try:
        build(bundle, wanted)
    except Exception as exc:
        log.debug("could not build %s: %s", BUNDLE_NAME, exc)
        return None
    return applet if applet.exists() else None


# -- building -----------------------------------------------------------------


def build(bundle: Path, marker: str | None = None) -> None:
    """Compile the notifier app into `bundle`, replacing whatever is there."""
    bundle.parent.mkdir(parents=True, exist_ok=True)
    # Built beside its destination, so the swap at the end is a rename on the same
    # filesystem, and a failure half way through leaves the old bundle untouched.
    with tempfile.TemporaryDirectory(dir=bundle.parent, prefix=".notifier-") as tmp:
        work = Path(tmp)
        source = work / "notifier.applescript"
        source.write_text(SOURCE, "utf-8")
        app = work / BUNDLE_NAME
        _run(["osacompile", "-o", str(app), str(source)])
        _install_icon(app, work)
        _rewrite_plist(app)
        (app / "Contents" / "Resources" / STAMP_FILE).write_text(
            marker if marker is not None else stamp(), "utf-8"
        )
        # Editing Info.plist and the icon broke the ad-hoc signature osacompile
        # applied, and macOS will not launch a bundle whose seal no longer matches
        # its contents. Sign last, over the finished thing.
        _run(["codesign", "--force", "--sign", "-", str(app)])
        _swap(app, bundle)
    _register(bundle)


def _install_icon(app: Path, work: Path) -> None:
    """Render the packaged PNG into the bundle's ``.icns``."""
    iconset = work / "logiswitch.iconset"
    iconset.mkdir()
    for size in ICON_SIZES:
        for name, pixels in ((f"icon_{size}x{size}", size), (f"icon_{size}x{size}@2x", size * 2)):
            out = iconset / f"{name}.png"
            _run(["sips", "-z", str(pixels), str(pixels), str(ICON), "--out", str(out)])
    resources = app / "Contents" / "Resources"
    _run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "applet.icns")])
    # osacompile also leaves the stock AppleScript icon in a compiled asset
    # catalogue, pointed at by CFBundleIconName (removed in _rewrite_plist). Both go,
    # so the only icon left in the bundle is ours.
    (resources / "Assets.car").unlink(missing_ok=True)


def _rewrite_plist(app: Path) -> None:
    path = app / "Contents" / "Info.plist"
    with path.open("rb") as handle:
        info = plistlib.load(handle)
    info.update(
        {
            # osacompile writes no identifier at all, and without one macOS has
            # nothing to hang a notification setting on: the notification is dropped.
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleName": DISPLAY_NAME,
            "CFBundleDisplayName": DISPLAY_NAME,
            "CFBundleIconFile": "applet",
            # A notification must not put an icon in the Dock or a window on screen;
            # this is an agent that runs for a fraction of a second.
            "LSUIElement": True,
        }
    )
    info.pop("CFBundleIconName", None)
    with path.open("wb") as handle:
        plistlib.dump(info, handle)


def _swap(built: Path, target: Path) -> None:
    """Put `built` where `target` is, keeping the old one until the new one lands."""
    previous = target.with_name(f"{target.name}.old")
    shutil.rmtree(previous, ignore_errors=True)
    if target.exists():
        os.rename(target, previous)
    os.rename(built, target)
    shutil.rmtree(previous, ignore_errors=True)


def _register(bundle: Path) -> None:
    """Tell LaunchServices about the bundle.

    Best effort, and only cosmetic: it makes the app appear in System Settings >
    Notifications immediately rather than after its first notification.
    """
    if not _LSREGISTER.exists():  # pragma: no cover - present on every macOS
        return
    try:
        _run([str(_LSREGISTER), "-f", str(bundle)])
    except Exception as exc:  # pragma: no cover - cosmetic either way
        log.debug("could not register %s with LaunchServices: %s", bundle, exc)


def _run(command: list[str]) -> None:
    subprocess.run(command, capture_output=True, timeout=BUILD_TIMEOUT, check=True)


# -- posting ------------------------------------------------------------------


def environment(body: str, title: str) -> dict[str, str]:
    """The applet's entire input.

    In the environment rather than in argv because an applet, unlike a script run by
    ``osascript``, is not given command-line arguments -- and because a value here is
    never re-parsed by anything on the way.
    """
    return {**os.environ, BODY_ENV: body, TITLE_ENV: title}


def show(applet: Path, body: str, title: str, timeout: float) -> None:
    """Post one notification. Raises if the applet does not run."""
    subprocess.run(
        [str(applet)],
        env=environment(body, title),
        capture_output=True,
        timeout=timeout,
        check=True,
    )
