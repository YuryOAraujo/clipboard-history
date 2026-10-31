import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from clipboard.history import HistoryStore


@unittest.skipUnless(os.environ.get("CLIPBOARD_HISTORY_TEST_ISOLATED_DISPLAY") == "1",
                     "Run tests/run_gui_tests.py to use an isolated display")
class ApplicationProcessTests(unittest.TestCase):
    def test_background_process_handles_shortcut_launches_and_restart(self):
        # Observe the real application lifecycle while exercising its normal CLI
        # in separate processes, on the private test display/session bus.
        script = """
import json
from pathlib import Path
import sys
from clipboard.clipboard_ui import Application

class ObservedApplication(Application):
    def snapshot(self):
        state = {"visible": self.window is not None and self.window.get_visible(),
                 "items": len(self.history.items),
                 "has_rows": self.rows is not None and self.rows.get_row_at_index(0) is not None}
        temporary = Path(sys.argv[1] + ".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(sys.argv[1])

    def do_startup(self):
        super().do_startup()
        self.snapshot()

    def do_activate(self):
        super().do_activate()
        self.snapshot()

raise SystemExit(ObservedApplication().run(["clipboard-history", "--background"]))
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = HistoryStore(root / "clipboard-history")
            history.add_text("first")
            history.add_text("second")
            snapshot = root / "state.json"
            environment = dict(os.environ, XDG_DATA_HOME=directory)

            def start():
                return subprocess.Popen([sys.executable, "-c", script, str(snapshot)],
                                        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            primary = start()

            def stop():
                if primary.poll() is None:
                    primary.terminate()
                primary.communicate(timeout=5)

            try:
                deadline = time.monotonic() + 5
                while not snapshot.exists():
                    if primary.poll() is not None:
                        self.fail(primary.communicate()[1].decode())
                    if time.monotonic() >= deadline:
                        self.fail("Background application did not start")
                    time.sleep(0.01)
                self.assertEqual(json.loads(snapshot.read_text()),
                                 {"visible": False, "items": 2, "has_rows": False})
                for arguments, visible in (([], True), (["--background"], True), ([], False)):
                    subprocess.run([sys.executable, "-m", "clipboard", *arguments], env=environment,
                                   check=True, capture_output=True, timeout=5)
                    self.assertEqual(json.loads(snapshot.read_text()),
                                     {"visible": visible, "items": 2, "has_rows": visible})
                stop()
                snapshot.unlink()
                primary = start()
                deadline = time.monotonic() + 5
                while not snapshot.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(json.loads(snapshot.read_text())["items"], 2)
            finally:
                stop()

    def test_launcher_activates_older_instance_without_command_line_support(self):
        script = """
from pathlib import Path
import sys
from gi.repository import Gio

class LegacyApplication(Gio.Application):
    count = 0

    def do_startup(self):
        Gio.Application.do_startup(self)
        self.hold()

    def do_activate(self):
        self.count += 1
        temporary = Path(sys.argv[1] + ".tmp")
        temporary.write_text(str(self.count))
        temporary.replace(sys.argv[1])

raise SystemExit(LegacyApplication(application_id="com.clipboard.history").run(["legacy-clipboard"]))
"""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "activations"
            primary = subprocess.Popen([sys.executable, "-c", script, str(snapshot)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not snapshot.exists():
                    if primary.poll() is not None:
                        self.fail(primary.communicate()[1].decode())
                    if time.monotonic() >= deadline:
                        self.fail("Legacy application did not start")
                    time.sleep(0.01)
                self.assertEqual(snapshot.read_text(), "1")
                subprocess.run([sys.executable, "-m", "clipboard"], check=True,
                               capture_output=True, timeout=5)
                self.assertEqual(snapshot.read_text(), "2")
            finally:
                if primary.poll() is None:
                    primary.terminate()
                primary.communicate(timeout=5)
