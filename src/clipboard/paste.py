"""Keyboard-only XDG RemoteDesktop session for pasting on Wayland and X11."""

import json
import logging
import os
from pathlib import Path
import tempfile
import uuid

from gi.repository import Gio, GLib


logger = logging.getLogger(__name__)
BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.RemoteDesktop"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
CONTROL = 0xFFE3
SHIFT = 0xFFE1
V = ord("v")


class PortalPaste:
    def __init__(self, directory: Path, on_state, *, enabled_by_default=True):
        self.settings_path = directory / "paste.json"
        self.on_state = on_state
        self.state = "disabled"
        self.connection = None
        self.session = None
        self._generation = 0
        self._cancellable = None
        self._request_handle = None
        self._request_subscription = None
        self._session_subscription = None
        self._pressed = []
        self._sending = False
        try:
            settings = json.loads(self.settings_path.read_text())
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        self.want_enabled = settings.get("enabled", True) is True and enabled_by_default
        token = settings.get("restore_token")
        self._restore_token = token if isinstance(token, str) else None

    def _set_state(self, state, message=""):
        self.state = state
        if self.on_state:
            self.on_state(state, message)

    def enable(self):
        if self.state in ("ready", "requesting"):
            return
        self.want_enabled = True
        self._generation += 1
        self._cancellable = Gio.Cancellable()
        self._set_state("requesting")
        Gio.bus_get(Gio.BusType.SESSION, self._cancellable, self._on_bus, self._generation)

    def _on_bus(self, _source, result, generation):
        try:
            connection = Gio.bus_get_finish(result)
        except GLib.Error as error:
            if generation == self._generation:
                self._fail(str(error))
            return
        if generation != self._generation:
            return
        self.connection = connection
        self._request("CreateSession", (), "(a{sv})", {
            "session_handle_token": GLib.Variant("s", "clipboard_" + uuid.uuid4().hex),
        }, self._on_created)

    def _request(self, method, arguments, signature, options, callback):
        generation = self._generation
        token = "clipboard_" + uuid.uuid4().hex
        sender = self.connection.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        options = dict(options, handle_token=GLib.Variant("s", token))
        self._request_handle = handle

        def response(_connection, _sender, _path, _interface, _signal, parameters):
            if generation != self._generation:
                return
            self._finish_request()
            code, results = parameters.unpack()
            if code == 0:
                callback(results)
            else:
                self._restore_token = None
                self.want_enabled = False
                self._teardown()
                self._save_settings()
                self._set_state("denied", "Keyboard access was not granted. Entries will still be copied.")

        # Subscribe before making the call: a fast portal may answer before its
        # method reply arrives, and losing Response would leave setup stuck.
        subscription = self.connection.signal_subscribe(
            BUS_NAME, REQUEST, "Response", handle, None, Gio.DBusSignalFlags.NONE, response)
        self._request_subscription = subscription

        def called(connection, result):
            try:
                returned_handle = connection.call_finish(result).unpack()[0]
            except GLib.Error as error:
                if generation == self._generation:
                    self._fail(str(error))
                return
            if generation != self._generation or self._request_subscription != subscription:
                return
            if returned_handle != handle:
                connection.signal_unsubscribe(subscription)
                self._request_handle = returned_handle
                self._request_subscription = connection.signal_subscribe(
                    BUS_NAME, REQUEST, "Response", returned_handle, None,
                    Gio.DBusSignalFlags.NONE, response)

        self.connection.call(BUS_NAME, OBJECT_PATH, INTERFACE, method,
                             GLib.Variant(signature, (*arguments, options)),
                             GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE,
                             10000, self._cancellable, called)

    def _on_created(self, results):
        session = results.get("session_handle")
        if not isinstance(session, str) or not GLib.Variant.is_object_path(session):
            self._fail("The desktop did not create a keyboard session.")
            return
        self.session = session
        self._session_subscription = self.connection.signal_subscribe(
            BUS_NAME, SESSION, "Closed", session, None, Gio.DBusSignalFlags.NONE,
            self._on_session_closed)
        options = {"types": GLib.Variant("u", 1), "persist_mode": GLib.Variant("u", 2)}
        if self._restore_token:
            options["restore_token"] = GLib.Variant("s", self._restore_token)
        self._request("SelectDevices", (session,), "(oa{sv})", options, self._on_selected)

    def _on_selected(self, _results):
        self._request("Start", (self.session, ""), "(osa{sv})", {}, self._on_started)

    def _on_started(self, results):
        if not results.get("devices", 0) & 1:
            self._fail("The desktop did not grant keyboard access.")
            return
        token = results.get("restore_token")
        self._restore_token = token if isinstance(token, str) else None
        self._save_settings()
        self._set_state("ready")

    def _on_session_closed(self, _connection, _sender, path, _interface, _signal, _parameters):
        if path != self.session:
            return
        # Respect GNOME's Stop button; do not silently request access again.
        self.want_enabled = False
        self.session = None
        self._teardown()
        self._save_settings()
        self._set_state("disabled", "Automatic paste was stopped. Entries will still be copied.")

    def paste(self, callback, *, shifted=False):
        if self.state != "ready" or self._sending:
            callback(False)
            return
        self._sending = True
        generation = self._generation
        keys = [CONTROL] + ([SHIFT] if shifted else []) + [V]
        events = [(key, 1) for key in keys] + [(key, 0) for key in reversed(keys)]

        def send(index):
            if generation != self._generation:
                return
            if index == len(events):
                self._sending = False
                callback(True)
                return
            key, state = events[index]
            # Track presses before dispatch, since cancellation may arrive
            # after the server applied a key but before its reply was received.
            if state:
                self._pressed.append(key)

            def sent(connection, result):
                try:
                    connection.call_finish(result)
                except GLib.Error as error:
                    if generation == self._generation:
                        logger.warning("Cannot send paste shortcut: %s", error)
                        self._release_keys()
                        self._sending = False
                        callback(False)
                    return
                if generation != self._generation:
                    return
                if not state and key in self._pressed:
                    self._pressed.remove(key)
                send(index + 1)

            self._notify_key(key, state, sent, self._cancellable)

        send(0)

    def _notify_key(self, key, state, callback=None, cancellable=None):
        self.connection.call(BUS_NAME, OBJECT_PATH, INTERFACE, "NotifyKeyboardKeysym",
                             GLib.Variant("(oa{sv}iu)", (self.session, {}, key, state)),
                             None, Gio.DBusCallFlags.NONE, 3000, cancellable, callback)

    def _release_keys(self):
        if self.connection is not None and self.session is not None:
            for key in reversed(self._pressed):
                self._notify_key(key, 0)
        self._pressed.clear()

    def _finish_request(self):
        if self._request_subscription is not None:
            self.connection.signal_unsubscribe(self._request_subscription)
            self._request_subscription = None
        self._request_handle = None

    def _teardown(self):
        self._generation += 1
        if self._cancellable is not None:
            self._cancellable.cancel()
        self._release_keys()
        if self.connection is not None:
            if self._request_handle is not None:
                self.connection.call(BUS_NAME, self._request_handle, REQUEST, "Close", None,
                                     None, Gio.DBusCallFlags.NONE, 1000, None, None)
            self._finish_request()
            if self._session_subscription is not None:
                self.connection.signal_unsubscribe(self._session_subscription)
                self._session_subscription = None
            if self.session is not None:
                self.connection.call(BUS_NAME, self.session, SESSION, "Close", None,
                                     None, Gio.DBusCallFlags.NONE, 1000, None, None)
        self.session = None
        self.connection = None
        self._sending = False

    def disable(self):
        self.want_enabled = False
        self._teardown()
        self._save_settings()
        self._set_state("disabled")

    def shutdown(self):
        self.on_state = None
        self._teardown()

    def _fail(self, message):
        logger.warning("Automatic paste unavailable: %s", message)
        self._teardown()
        self.want_enabled = False
        self._save_settings()
        self._set_state("unavailable", "Automatic paste is unavailable. Entries will still be copied.")

    def _save_settings(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.settings_path.parent,
                                             prefix=".paste-", delete=False) as file:
                temporary = Path(file.name)
                json.dump({"enabled": self.want_enabled, "restore_token": self._restore_token}, file)
            os.replace(temporary, self.settings_path)
        except OSError:
            logger.warning("Cannot save automatic paste settings", exc_info=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
