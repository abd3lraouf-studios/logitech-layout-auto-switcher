"""The flag that keeps helper tools from flashing consoles on the desktop.

The agent runs without a console of its own: pythonw.exe on Windows, launchd
on macOS. Windows still allocates a console window for every console-subsystem
child of a console-less parent -- ``tasklist`` during arbitration rechecks,
``pip`` during a self-update, ``sc`` in service mode -- and that window lives
exactly as long as the child. Redirected pipes do not prevent the allocation,
so without this flag each of those calls is a terminal flashing open and shut
on the user's desktop.
"""

from __future__ import annotations

import subprocess
import sys

if sys.platform == "win32":
    #: Passed as ``creationflags`` to every ``subprocess.run`` the agent makes.
    CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    #: POSIX has no console to suppress, and Popen ignores the flag there.
    CREATE_NO_WINDOW = 0
