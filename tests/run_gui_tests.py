"""Run GTK tests on a private Broadway display and D-Bus session."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    for command in ("gtk4-broadwayd", "dbus-run-session"):
        if shutil.which(command) is None:
            raise SystemExit(f"GUI tests require {command}")
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="clipboard-broadway-") as directory:
        runtime = Path(directory)
        environment = dict(os.environ, XDG_RUNTIME_DIR=directory,
                           GDK_BACKEND="broadway", BROADWAY_DISPLAY=":5",
                           GTK_A11Y="none", GSETTINGS_BACKEND="memory", GIO_USE_VFS="local",
                           PYTHONPATH=str(root / "src"), CLIPBOARD_HISTORY_TEST_ISOLATED_DISPLAY="1",
                           CLIPBOARD_HISTORY_AUTO_PASTE="0")
        daemon = subprocess.Popen(
            ["gtk4-broadwayd", "--unixsocket", str(runtime / "http.socket"), ":5"],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                if daemon.poll() is not None:
                    raise RuntimeError(daemon.stdout.read().decode())
                if list(runtime.glob("broadway*.socket")):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Broadway display did not start")
            result = subprocess.run(
                ["dbus-run-session", "--", sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                env=environment, cwd=root,
            )
        finally:
            if daemon.poll() is None:
                daemon.terminate()
            daemon.communicate(timeout=5)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
