"""Detached launcher for relay.json (controller integration).

Starts wb_proxy.py as an independent background process (log redirected to
relay.log) and exits immediately, so relays.start_relay()'s subprocess.run
returns as soon as the server is spawned. The stop path kills by port +
cmdline_match (wb_proxy[.]py), which still matches the detached child.

Do NOT add DETACHED_PROCESS here: the venv launcher (Scripts\\python.exe)
spawns the real interpreter as a child without forwarding creation flags, so
a detached (console-less) parent makes the child create a *visible* console
window. Closing that window fires CTRL_CLOSE_EVENT and wb_proxy.py exits.
CREATE_NO_WINDOW keeps a hidden console that the child inherits instead.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "wb_proxy.py")
LOG = os.path.join(HERE, "relay.log")
PORT = "8788"

CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    if not os.path.isfile(SCRIPT):
        print(f"[relay_start] wb_proxy.py not found: {SCRIPT}")
        return 1
    log = open(LOG, "ab")
    try:
        subprocess.Popen(
            [sys.executable, SCRIPT, "--port", PORT],
            cwd=HERE,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    finally:
        log.close()
    print(f"[relay_start] spawned wb_proxy.py on port {PORT}, log: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
