from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from clipboard.clipboard_ui import Application, ClipboardEntry, Gdk, Gio, GLib, GObject, Gtk
from clipboard.history import HistoryStore


class FakeClipboard:
    """A controllable async clipboard, including delayed/cancelled completions."""

    def __init__(self):
        self.formats = Gdk.ContentProvider.new_for_value("text").ref_formats()
        self.provider = None
        self.requests = []
        self.on_change = None

    def get_formats(self):
        return self.formats

    def get_content(self):
        return self.provider

    def set_content(self, provider):
        self.provider = provider
        self.formats = provider.ref_formats() if provider else Gdk.ContentFormats.new([])
        if self.on_change:
            self.on_change(self)
        return True

    def read_text_async(self, cancellable, callback, version):
        self.requests.append(("text", cancellable, callback, version))

    def read_texture_async(self, cancellable, callback, version):
        self.requests.append(("image", cancellable, callback, version))

    def read_text_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    read_texture_finish = read_text_finish

    def complete(self, index, result):
        _kind, _cancellable, callback, version = self.requests[index]
        callback(self, result, version)


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.history = HistoryStore(Path(temporary.name))
        self.app = Application(history=self.history)
        self.clipboard = FakeClipboard()
        self.app.clipboard = self.clipboard
        self.clipboard.on_change = self.app.on_clipboard_changed

    def test_native_text_type_is_captured_without_mime_types(self):
        self.assertEqual(self.clipboard.get_formats().get_mime_types(), [])
        self.app.on_clipboard_changed(self.clipboard)
        self.clipboard.complete(0, "copied text")
        self.assertEqual(self.history.items[0].text, "copied text")

    def test_new_change_cancels_old_read_and_discards_late_result(self):
        self.app.on_clipboard_changed(self.clipboard)
        self.app.on_clipboard_changed(self.clipboard)
        self.assertTrue(self.clipboard.requests[0][1].is_cancelled())
        self.clipboard.complete(1, "new")
        self.clipboard.complete(0, "old")
        self.assertEqual([item.text for item in self.history.items], ["new"])

    def test_clear_rejects_pending_result_and_persists_empty_history(self):
        self.history.add_text("old")
        self.app.on_clipboard_changed(self.clipboard)
        self.app.clear_all()
        self.clipboard.complete(0, "pending")
        self.assertEqual(HistoryStore(self.history.directory).items, [])
        self.assertEqual(self.clipboard.get_formats().get_mime_types(), [])

    def test_delete_current_clears_clipboard_and_stays_deleted(self):
        self.app.on_clipboard_changed(self.clipboard)
        self.clipboard.complete(0, "delete me")
        self.app.delete_item(self.history.items[0])
        self.assertEqual(HistoryStore(self.history.directory).items, [])
        self.assertIs(self.clipboard.get_content(), self.app._owned_provider)
        self.assertEqual(self.clipboard.get_formats().get_gtypes(), [GObject.TYPE_STRING])

    def test_delete_while_initial_read_is_pending_cannot_resurrect_item(self):
        item = self.history.add_text("delete me")
        self.app.on_clipboard_changed(self.clipboard)
        self.app.delete_item(item)
        self.clipboard.complete(0, "delete me")
        self.assertEqual(HistoryStore(self.history.directory).items, [])
        self.assertIs(self.clipboard.get_content(), self.app._owned_provider)

    def test_delete_old_item_does_not_drop_unrelated_pending_copy(self):
        item = self.history.add_text("old")
        self.app.on_clipboard_changed(self.clipboard)
        self.app.delete_item(item)
        self.clipboard.complete(0, "new copy")
        self.assertEqual([item.text for item in self.history.items], ["new copy"])

    def test_selecting_history_does_not_ignore_the_next_external_copy(self):
        item = self.history.add_text("selected")
        self.app.handle_row_click(None, SimpleNamespace(item=item))
        self.assertEqual(len(self.clipboard.requests), 0)
        self.clipboard.set_content(Gdk.ContentProvider.new_for_value("external"))
        self.clipboard.complete(0, "external")
        self.assertEqual([item.text for item in self.history.items], ["external", "selected"])

    def test_read_failure_does_not_crash_or_change_saved_history(self):
        item = self.history.add_text("saved")
        self.app.on_clipboard_changed(self.clipboard)
        self.clipboard.complete(0, GLib.Error("source closed"))
        self.assertEqual(self.history.items, [item])

    def test_image_failure_falls_back_to_available_text(self):
        self.clipboard.formats = Gdk.ContentFormats.new(["image/png", "text/plain"])
        self.app.on_clipboard_changed(self.clipboard)
        self.assertEqual(self.clipboard.requests[0][0], "image")
        self.clipboard.complete(0, GLib.Error("unsupported image"))
        self.assertEqual(self.clipboard.requests[1][0], "text")
        self.clipboard.complete(1, "fallback")
        self.assertEqual(self.history.items[0].text, "fallback")

    def test_image_is_saved_and_selected_as_original_png_bytes(self):
        texture = Gdk.MemoryTexture.new(2, 1, Gdk.MemoryFormat.R8G8B8A8,
                                       GLib.Bytes.new(bytes([255, 0, 0, 255]) * 2), 8)
        self.clipboard.formats = Gdk.ContentFormats.new(["image/png"])
        self.app.on_clipboard_changed(self.clipboard)
        self.clipboard.complete(0, texture)
        item = self.history.items[0]
        png = self.history.image_path(item).read_bytes()
        self.assertEqual(png, texture.save_to_png_bytes().get_data())
        self.app.handle_row_click(None, SimpleNamespace(item=item))
        self.assertEqual(self.clipboard.provider.ref_formats().get_mime_types(), ["image/png"])
        self.assertEqual(self.history.items, [item])

    def test_background_command_does_not_toggle_window(self):
        command_line = Mock()
        command_line.get_options_dict().contains.return_value = True
        with patch.object(self.app, "activate") as activate:
            self.assertEqual(self.app.do_command_line(command_line), 0)
            activate.assert_not_called()
        command_line.get_options_dict().contains.return_value = False
        with patch.object(self.app, "activate") as activate:
            self.app.do_command_line(command_line)
            activate.assert_called_once()

    def test_existing_instance_uses_activation_without_forwarding_command_line(self):
        options = Mock()
        options.contains.return_value = False
        with patch.object(self.app, "register", return_value=True), \
                patch.object(self.app, "get_is_remote", return_value=True), \
                patch.object(self.app, "activate") as activate:
            self.assertEqual(self.app.do_handle_local_options(options), 0)
            activate.assert_called_once()
            activate.reset_mock()
            options.contains.return_value = True
            self.assertEqual(self.app.do_handle_local_options(options), 0)
            activate.assert_not_called()

    def test_clear_does_not_release_selection_to_a_restoring_clipboard_manager(self):
        original_set = self.clipboard.set_content

        def set_content(provider):
            if provider is None:
                original_set(Gdk.ContentProvider.new_for_value("restored old entry"))
                self.app.on_read_text(self.clipboard, "restored old entry", self.app._read_version)
                return True
            return original_set(provider)

        self.clipboard.set_content = set_content
        self.history.add_text("old entry")
        self.app.clear_all()
        self.app.on_clipboard_changed(self.clipboard)
        self.assertEqual(self.history.items, [])
        self.assertEqual(self.clipboard.requests, [])

    def test_selection_hides_before_sending_paste_and_reopening_cancels_it(self):
        self.app.paste_backend = Mock(state="ready")
        self.app.window = Mock()
        self.app.window.get_visible.return_value = True
        item = self.history.add_text("paste me")
        with patch("clipboard.clipboard_ui.GLib.timeout_add", return_value=123) as timeout:
            self.app.handle_row_click(None, SimpleNamespace(item=item))
        self.app.window.set_visible.assert_called_with(False)
        self.app.paste_backend.paste.assert_not_called()
        callback, provider, shifted = timeout.call_args.args[1:]
        self.app.window.get_visible.return_value = False
        callback(provider, shifted)
        self.app.paste_backend.paste.assert_called_once_with(self.app._on_pasted, shifted=False)
        self.app.paste_backend.paste.reset_mock()
        self.app.window.get_visible.return_value = True
        callback(provider, shifted)
        self.app.paste_backend.paste.assert_not_called()

    def test_new_clipboard_content_cancels_a_delayed_paste(self):
        self.app.paste_backend = Mock(state="ready")
        item = self.history.add_text("selected")
        with patch("clipboard.clipboard_ui.GLib.timeout_add", return_value=123):
            self.app.handle_row_click(None, SimpleNamespace(item=item))
        with patch("clipboard.clipboard_ui.GLib.source_remove") as remove:
            self.clipboard.set_content(Gdk.ContentProvider.new_for_value("another copy"))
            remove.assert_called_once_with(123)
        self.assertIsNone(self.app._paste_timeout)
        self.app.paste_backend.paste.assert_not_called()

    def test_ready_paste_session_does_not_prevent_hiding_on_focus_loss(self):
        self.app.paste_backend = Mock(state="ready", want_enabled=True)
        self.app.window = Mock()
        self.app.window.get_visible.return_value = True
        self.app.window.is_active.return_value = True
        self.app._on_paste_state("ready")
        self.app.window.is_active.return_value = False
        self.app.handle_active_change(self.app.window, None)
        self.app.window.set_visible.assert_called_once_with(False)


Gtk.init_check()


@unittest.skipUnless(
    os.environ.get("CLIPBOARD_HISTORY_TEST_ISOLATED_DISPLAY") == "1" and Gdk.Display.get_default() is not None,
    "Run tests/run_gui_tests.py to use an isolated display",
)
class WindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.history = HistoryStore(Path(temporary.name))
        self.app = Application(history=self.history)
        self.app.set_application_id(f"com.clipboard.history.tests.t{id(self):x}")
        self.app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        self.app.register(None)
        self.addCleanup(self.app.clipboard.disconnect, self.app._clipboard_handler)
        self.addCleanup(self.app._invalidate_read)
        self.addCleanup(self.app.quit)
        self.addCleanup(lambda: self.app.window.destroy() if self.app.window else None)

    def test_first_activation_shows_and_reopening_restores_rows(self):
        self.history.add_text("keep")
        self.app.activate()
        self.assertTrue(self.app.window.get_visible())
        self.assertEqual(self.app.rows.get_row_at_index(0).item.text, "keep")
        self.app.handle_close_request(self.app.window)
        self.assertFalse(self.app.window.get_visible())
        self.assertIsNone(self.app.rows.get_row_at_index(0))
        self.app.activate()
        self.assertEqual(self.app.rows.get_row_at_index(0).item.text, "keep")

    def test_image_preview_is_scaled_and_hidden_rows_are_released(self):
        texture = Gdk.MemoryTexture.new(640, 480, Gdk.MemoryFormat.R8G8B8A8,
                                       GLib.Bytes.new(bytes([255, 0, 0, 255]) * 640 * 480), 640 * 4)
        item = self.history.add_image(texture.save_to_png_bytes().get_data())
        self.app.activate()
        entry = self.app.rows.get_row_at_index(0)
        self.assertIsInstance(entry, ClipboardEntry)
        picture = entry.get_child().get_first_child().get_first_child()
        preview = picture.get_paintable()
        self.assertLessEqual(preview.get_intrinsic_width(), 160)
        self.assertLessEqual(preview.get_intrinsic_height(), 100)
        self.app._hide_window()
        self.assertIsNone(self.app.rows.get_row_at_index(0))
        self.assertTrue(self.history.image_path(item).exists())

    def test_real_clipboard_text_and_image_are_captured_without_a_window(self):
        context = GLib.MainContext.default()

        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("Clipboard capture did not finish")

        self.app.clipboard.set("live clipboard text")
        wait_for(lambda: self.history.items and self.history.items[0].text == "live clipboard text")
        texture = Gdk.MemoryTexture.new(2, 1, Gdk.MemoryFormat.R8G8B8A8,
                                       GLib.Bytes.new(bytes([255, 0, 0, 255]) * 2), 8)
        self.app.clipboard.set_content(Gdk.ContentProvider.new_for_value(texture))
        wait_for(lambda: self.history.items and self.history.items[0].kind == "image")
        self.assertIsNone(self.app.window)
        image = self.history.items[0]
        self.assertEqual(self.history.image_path(image).read_bytes(), texture.save_to_png_bytes().get_data())
        # Leave the isolated test display empty for the next application.
        self.app.clear_all()

    def test_clear_all_replaces_os_clipboard_with_empty_text_and_stays_empty(self):
        self.app.clipboard.set("old OS clipboard entry")
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        while not self.history.items and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)
        self.assertTrue(self.history.items)
        self.app.clear_all()
        read = []

        def received(clipboard, result):
            read.append(clipboard.read_text_finish(result))

        self.app.clipboard.read_text_async(None, received)
        deadline = time.monotonic() + 2
        while not read and time.monotonic() < deadline:
            context.iteration(False)
            time.sleep(0.01)
        self.assertEqual(read, [""])
        self.app.activate()
        self.app.handle_close_request(self.app.window)
        self.app.activate()
        while context.pending():
            context.iteration(False)
        self.assertEqual(self.history.items, [])
        self.assertIsNone(self.app.rows.get_row_at_index(0))
        self.assertEqual(HistoryStore(self.history.directory).items, [])


if __name__ == "__main__":
    unittest.main()
