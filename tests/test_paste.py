from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from clipboard.paste import CONTROL, SHIFT, V, GLib, PortalPaste, REQUEST, SESSION


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.subscriptions = {}
        self.next_id = 0

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, interface, member, path, arg0, flags, callback):
        self.next_id += 1
        self.subscriptions[self.next_id] = (interface, member, path, callback)
        return self.next_id

    def signal_unsubscribe(self, identifier):
        del self.subscriptions[identifier]

    def call(self, name, path, interface, method, parameters, reply_type, flags,
             timeout, cancellable, callback):
        self.calls.append({"method": method, "path": path, "parameters": parameters,
                           "callback": callback, "completed": False})

    def call_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def emit(self, interface, signal, path, parameters):
        for kind, member, subscribed_path, callback in list(self.subscriptions.values()):
            if (kind, member, subscribed_path) == (interface, signal, path):
                callback(self, "portal", path, interface, signal, parameters)

    def pending(self, method):
        return next(call for call in self.calls if call["method"] == method and not call["completed"])

    def complete(self, method, result=None):
        call = self.pending(method)
        call["completed"] = True
        call["callback"](self, result if result is not None else GLib.Variant("()", ()))

    def respond(self, method, results=None, *, code=0, before_reply=False):
        call = self.pending(method)
        token = call["parameters"].unpack()[-1]["handle_token"]
        path = f"/org/freedesktop/portal/desktop/request/1_42/{token}"

        def response():
            self.emit(REQUEST, "Response", path, GLib.Variant("(ua{sv})", (code, results or {})))

        if before_reply:
            response()
        self.complete(method, GLib.Variant("(o)", (path,)))
        if not before_reply:
            response()


class PasteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.states = Mock()
        self.paste = PortalPaste(self.directory, self.states)
        self.bus = FakeConnection()
        self.addCleanup(self.paste.shutdown)

    def connect(self):
        with patch("clipboard.paste.Gio.bus_get") as get_bus:
            self.paste.enable()
        callback, generation = get_bus.call_args.args[-2:]
        with patch("clipboard.paste.Gio.bus_get_finish", return_value=self.bus):
            callback(None, object(), generation)

    def ready(self, *, before_reply=False):
        self.connect()
        self.bus.respond("CreateSession", {"session_handle": GLib.Variant("o", "/session/test")},
                         before_reply=before_reply)
        self.bus.respond("SelectDevices", before_reply=before_reply)
        self.bus.respond("Start", {"devices": GLib.Variant("u", 1),
                                   "restore_token": GLib.Variant("s", "new-token")},
                         before_reply=before_reply)

    def test_keyboard_only_permission_and_early_responses(self):
        self.ready(before_reply=True)
        self.assertEqual(self.paste.state, "ready")
        options = next(call for call in self.bus.calls if call["method"] == "SelectDevices")["parameters"].unpack()[1]
        self.assertEqual(options["types"], 1)
        self.assertEqual(options["persist_mode"], 2)
        self.assertEqual(len(self.bus.subscriptions), 1)  # Session::Closed only.
        self.assertEqual(self.paste.settings_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(call["method"] == "NotifyKeyboardKeysym" for call in self.bus.calls))

    def test_permission_denial_sends_no_keys_and_keeps_copy_only_mode(self):
        self.connect()
        self.bus.respond("CreateSession", {"session_handle": GLib.Variant("o", "/session/test")})
        self.bus.respond("SelectDevices")
        self.bus.respond("Start", code=1)
        self.assertEqual(self.paste.state, "denied")
        self.assertFalse(self.paste.want_enabled)
        callback = Mock()
        self.paste.paste(callback)
        callback.assert_called_once_with(False)
        self.assertFalse(any(call["method"] == "NotifyKeyboardKeysym" for call in self.bus.calls))

    def test_paste_keys_are_ordered_and_all_released(self):
        self.ready()
        callback = Mock()
        self.paste.paste(callback)
        for _ in range(4):
            self.bus.complete("NotifyKeyboardKeysym")
        events = [call["parameters"].unpack()[2:] for call in self.bus.calls
                  if call["method"] == "NotifyKeyboardKeysym"]
        self.assertEqual(events, [(CONTROL, 1), (V, 1), (V, 0), (CONTROL, 0)])
        callback.assert_called_once_with(True)
        self.assertEqual(self.paste._pressed, [])

    def test_shifted_paste_supports_terminal_shortcut(self):
        self.ready()
        callback = Mock()
        self.paste.paste(callback, shifted=True)
        for _ in range(6):
            self.bus.complete("NotifyKeyboardKeysym")
        events = [call["parameters"].unpack()[2:] for call in self.bus.calls
                  if call["method"] == "NotifyKeyboardKeysym"]
        self.assertEqual(events, [(CONTROL, 1), (SHIFT, 1), (V, 1),
                                  (V, 0), (SHIFT, 0), (CONTROL, 0)])

    def test_key_failure_releases_pressed_keys(self):
        self.ready()
        callback = Mock()
        self.paste.paste(callback)
        self.bus.complete("NotifyKeyboardKeysym")
        with self.assertLogs("clipboard.paste", level="WARNING"):
            self.bus.complete("NotifyKeyboardKeysym", GLib.Error("input stopped"))
        callback.assert_called_once_with(False)
        events = [call["parameters"].unpack()[2:] for call in self.bus.calls
                  if call["method"] == "NotifyKeyboardKeysym"]
        self.assertEqual(events[-2:], [(V, 0), (CONTROL, 0)])
        self.assertEqual(self.paste._pressed, [])

    def test_cancelling_during_key_press_releases_modifier_and_ignores_late_reply(self):
        self.ready()
        callback = Mock()
        self.paste.paste(callback)
        self.paste.disable()
        self.bus.complete("NotifyKeyboardKeysym")
        events = [call["parameters"].unpack()[2:] for call in self.bus.calls
                  if call["method"] == "NotifyKeyboardKeysym"]
        self.assertEqual(events, [(CONTROL, 1), (CONTROL, 0)])
        callback.assert_not_called()

    def test_session_stop_disables_auto_paste_without_reconnecting(self):
        self.ready()
        self.bus.emit(SESSION, "Closed", "/session/test", GLib.Variant("()", ()))
        self.assertEqual(self.paste.state, "disabled")
        self.assertFalse(self.paste.want_enabled)
        self.assertIsNone(self.paste.session)

    def test_restores_granted_permission_on_later_run(self):
        self.ready()
        self.paste.shutdown()
        self.paste = PortalPaste(self.directory, self.states)
        self.connect()
        self.bus.respond("CreateSession", {"session_handle": GLib.Variant("o", "/session/second")})
        options = self.bus.pending("SelectDevices")["parameters"].unpack()[1]
        self.assertEqual(options["restore_token"], "new-token")

    def test_disabling_during_setup_closes_request_and_rejects_late_response(self):
        self.connect()
        subscription = next(iter(self.bus.subscriptions.values()))
        self.paste.disable()
        callback = subscription[-1]
        callback(self.bus, "portal", subscription[2], REQUEST, "Response",
                 GLib.Variant("(ua{sv})", (0, {"session_handle": GLib.Variant("o", "/session/late")})))
        self.assertEqual(self.paste.state, "disabled")
        self.assertIsNone(self.paste.session)

    def test_portal_failure_keeps_clipboard_app_usable(self):
        self.connect()
        with self.assertLogs("clipboard.paste", level="WARNING"):
            self.bus.complete("CreateSession", GLib.Error("portal unavailable"))
        self.assertEqual(self.paste.state, "unavailable")
        self.assertFalse(self.paste.want_enabled)

    def test_late_close_signal_from_an_old_session_does_not_disable_new_session(self):
        self.ready()
        old_callback = next(iter(self.bus.subscriptions.values()))[-1]
        self.paste.session = "/session/replacement"
        old_callback(self.bus, "portal", "/session/test", SESSION, "Closed", GLib.Variant("()", ()))
        self.assertEqual(self.paste.state, "ready")
        self.assertEqual(self.paste.session, "/session/replacement")


if __name__ == "__main__":
    unittest.main()
