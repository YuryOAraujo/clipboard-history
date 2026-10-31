import logging
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango

from .history import ClipboardItem, HistoryStore, content_id
from .paste import PortalPaste
from .screen_dimensions import calculate_window_size


logger = logging.getLogger(__name__)


class ClipboardEntry(Gtk.ListBoxRow):
    def __init__(self, item: ClipboardItem, history: HistoryStore, on_delete):
        super().__init__()
        self.item = item
        row = Gtk.Box(spacing=12, margin_start=8, margin_bottom=8, margin_top=8,
                      margin_end=8, valign=Gtk.Align.START)
        if item.kind == "text":
            # Only the preview is shortened; copying keeps the complete text.
            row.append(Gtk.Label(label=(item.text or "")[:500], hexpand=True,
                                 xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                 single_line_mode=True))
        else:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(history.image_path(item)), 160, 100, True)
                picture = Gtk.Picture.new_for_pixbuf(pixbuf)
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_can_shrink(True)
                picture.set_size_request(160, 100)
                preview = Gtk.Box(hexpand=True)
                preview.append(picture)
                row.append(preview)
            except GLib.Error:
                row.append(Gtk.Label(label="Image preview unavailable", hexpand=True))
                logger.warning("Cannot preview clipboard image %s", item.id)

        delete_button = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.START)
        delete_button.set_tooltip_text("Delete from history")
        delete_button.connect("clicked", lambda _button: on_delete(item))
        row.append(delete_button)
        self.set_child(row)


class Application(Gtk.Application):
    def __init__(self, *args, history: HistoryStore | None = None, paste_backend=None, **kwargs):
        super().__init__(*args, application_id="com.clipboard.history",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE, **kwargs)
        self.history = history
        self.window = None
        self.rows = None
        self.clipboard = None
        self._clipboard_handler = None
        self._read_version = 0
        self._read_cancellable = None
        self._current_item_id = None
        self._owned_provider = None
        self._deleted_during_read = set()
        self.paste_backend = paste_backend
        self._paste_timeout = None
        self._awaiting_popup_focus = False
        self._updating_paste_toggle = False
        self.paste_toggle = None
        self.status_label = None
        self.add_main_option("background", 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, "Monitor without opening the window", None)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        if self.history is None:
            self.history = HistoryStore()
        if self.paste_backend is None:
            self.paste_backend = PortalPaste(
                self.history.directory, self._on_paste_state,
                enabled_by_default=os.environ.get("CLIPBOARD_HISTORY_AUTO_PASTE") != "0")
        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("Clipboard History requires a graphical session")
        self.clipboard = display.get_clipboard()
        self._clipboard_handler = self.clipboard.connect("changed", self.on_clipboard_changed)
        # Monitoring belongs to the application, even when no window exists.
        self.hold()
        self.on_clipboard_changed(self.clipboard)

    def do_command_line(self, command_line):
        if not command_line.get_options_dict().contains("background"):
            self.activate()
        return 0

    def do_handle_local_options(self, options):
        # Use the standard Activate method for an existing instance. Older
        # versions do not implement the remote CommandLine method, and GTK's
        # Activate path also forwards the desktop's launch context.
        if not self.register(None):
            return 1
        if self.get_is_remote():
            if not options.contains("background"):
                self.activate()
            return 0
        return -1

    def do_shutdown(self):
        self._cancel_pending_paste()
        if self.paste_backend is not None:
            self.paste_backend.shutdown()
        self._invalidate_read()
        if self.clipboard is not None and self._clipboard_handler is not None:
            self.clipboard.disconnect(self._clipboard_handler)
            self._clipboard_handler = None
        Gtk.Application.do_shutdown(self)

    def create_window(self):
        self.window = Gtk.ApplicationWindow(application=self, title="Clipboard")
        self.window.set_resizable(False)
        self.window.set_default_size(*calculate_window_size(self.window.get_display()))
        self.rows = Gtk.ListBox()
        self.rows.set_selection_mode(Gtk.SelectionMode.NONE)
        self.rows.set_placeholder(Gtk.Label(label="Your clipboard history is empty"))

        header = Gtk.HeaderBar()
        header.set_decoration_layout("icon:close")
        header.pack_start(Gtk.Label(label="Clipboard", hexpand=True, xalign=0.10))
        clear_button = Gtk.Button(label="Clear All")
        clear_button.connect("clicked", self.clear_all)
        header.pack_end(clear_button)
        self.paste_toggle = Gtk.ToggleButton(label="Auto-paste")
        self.paste_toggle.set_tooltip_text(
            "Paste into the previous app when selecting an entry. Requires keyboard access.")
        if self.paste_backend is not None:
            self.paste_toggle.set_active(self.paste_backend.want_enabled)
        self.paste_toggle.connect("toggled", self._toggle_paste)
        header.pack_end(self.paste_toggle)
        self.window.set_titlebar(header)

        panel = Gtk.ScrolledWindow(margin_bottom=6, margin_end=12,
                                  margin_start=12, margin_top=6)
        panel.set_child(self.rows)
        panel.set_vexpand(True)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(panel)
        self.status_label = Gtk.Label(wrap=True, xalign=0, margin_start=12,
                                     margin_end=12, margin_bottom=8)
        layout.append(self.status_label)
        self.window.set_child(layout)
        if self.paste_backend is not None:
            self._on_paste_state(self.paste_backend.state)
        self.rows.connect("row-activated", self.handle_row_click)
        self.window.connect("close-request", self.handle_close_request)
        self.window.connect("notify::is-active", self.handle_active_change)

    def do_activate(self):
        self._cancel_pending_paste()
        if self.window is None:
            self.create_window()
        if self.window.get_visible():
            self._hide_window()
        else:
            self._refresh_rows()
            self._awaiting_popup_focus = not self.window.is_active()
            self.window.present()
            if self.paste_backend is not None and self.paste_backend.want_enabled:
                self.paste_backend.enable()

    def _toggle_paste(self, button):
        if self._updating_paste_toggle or self.paste_backend is None:
            return
        self._cancel_pending_paste()
        if button.get_active():
            self.paste_backend.enable()
        else:
            self.paste_backend.disable()

    def _set_status(self, message):
        if self.status_label is not None:
            self.status_label.set_text(message)

    def _on_paste_state(self, state, message=""):
        if self.paste_toggle is not None:
            self._updating_paste_toggle = True
            self.paste_toggle.set_active(self.paste_backend.want_enabled)
            self._updating_paste_toggle = False
        messages = {
            "disabled": "Auto-paste is off. Select an entry to copy it.",
            "requesting": "Allow keyboard access in GNOME to enable automatic paste.",
            "ready": "Select an entry to paste. Hold Shift for Ctrl+Shift+V.",
        }
        self._set_status(message or messages.get(state, "Select an entry to copy it."))
        if state != "requesting" and self.window is not None and self.window.get_visible():
            # Return focus from the permission dialog, without pasting anything
            # as a delayed consequence of approving that dialog.
            self._awaiting_popup_focus = not self.window.is_active()
            self.window.present()

    def _remove_rows(self):
        if self.rows is not None:
            while (row := self.rows.get_row_at_index(0)) is not None:
                self.rows.remove(row)

    def _refresh_rows(self):
        if self.rows is None or self.history is None:
            return
        self._remove_rows()
        for item in self.history.items:
            self.rows.append(ClipboardEntry(item, self.history, self.delete_item))

    def _refresh_visible_rows(self):
        if self.window is not None and self.window.get_visible():
            self._refresh_rows()

    def _hide_window(self):
        if self.window is not None:
            self.window.set_visible(False)
            # Release previews while monitoring in the background.
            self._remove_rows()

    def handle_close_request(self, _window):
        self._cancel_pending_paste()
        self._hide_window()
        return True

    def handle_active_change(self, window, _param):
        if window.is_active():
            self._awaiting_popup_focus = False
        elif self.paste_backend is not None and self.paste_backend.state == "requesting":
            return
        elif not self._awaiting_popup_focus:
            self._hide_window()

    def delete_item(self, item: ClipboardItem):
        assert self.history is not None
        try:
            self.history.remove(item.id)
        except OSError:
            logger.exception("Cannot delete clipboard history item")
            return
        # A pending read may contain this item, or an unrelated new copy.
        # Reject only the deleted content when that read completes.
        self._deleted_during_read.add(item.id)
        if self._current_item_id == item.id:
            self._invalidate_read()
            self._clear_clipboard()
        self._refresh_visible_rows()

    def clear_all(self, _widget=None):
        self._cancel_pending_paste()
        assert self.history is not None
        try:
            self.history.clear()
        except OSError:
            logger.exception("Cannot clear clipboard history")
            return
        self._invalidate_read()
        self._clear_clipboard()
        self._refresh_visible_rows()

    def _clear_clipboard(self):
        self._cancel_pending_paste()
        self._current_item_id = None
        # Keep an empty selection owned by us. Releasing ownership can let a
        # desktop clipboard manager restore its previous selection on Wayland.
        provider = Gdk.ContentProvider.new_for_value("")
        old_provider = self._owned_provider
        self._owned_provider = provider
        if self.clipboard is not None and not self.clipboard.set_content(provider):
            self._owned_provider = old_provider
            self._set_status("History was cleared, but the system clipboard could not be cleared.")
            logger.warning("Cannot clear the system clipboard")

    def handle_row_click(self, _listbox, row: ClipboardEntry):
        assert self.clipboard is not None
        assert self.history is not None
        item = row.item
        try:
            if item.kind == "text":
                provider = Gdk.ContentProvider.new_for_value(item.text)
            else:
                png = self.history.image_path(item).read_bytes()
                provider = Gdk.ContentProvider.new_for_bytes("image/png", GLib.Bytes.new(png))
        except (OSError, GLib.Error):
            logger.exception("Cannot copy clipboard history item")
            return
        self._cancel_pending_paste()
        self._invalidate_read()
        old_provider, old_item_id = self._owned_provider, self._current_item_id
        self._owned_provider, self._current_item_id = provider, item.id
        if self.clipboard.set_content(provider):
            shifted = False
            if self.paste_backend is not None and self.paste_backend.state == "ready":
                display = Gdk.Display.get_default()
                seat = display.get_default_seat() if display else None
                keyboard = seat.get_keyboard() if seat else None
                modifiers = keyboard.get_modifier_state() if keyboard else Gdk.ModifierType(0)
                shifted = bool(modifiers & Gdk.ModifierType.SHIFT_MASK)
                if modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                                | Gdk.ModifierType.SUPER_MASK):
                    self._set_status("Release Ctrl, Alt, and Super, then select the entry again to paste.")
                    return
            self._hide_window()
            if self.paste_backend is not None and self.paste_backend.state == "ready":
                # Let the compositor restore focus to the underlying app before
                # sending the shortcut. Reopening or copying something else
                # cancels this pending action.
                self._paste_timeout = GLib.timeout_add(150, self._paste_selected, provider, shifted)
        else:
            self._owned_provider, self._current_item_id = old_provider, old_item_id
            logger.warning("Cannot set the system clipboard")

    def _cancel_pending_paste(self):
        if self._paste_timeout is not None:
            GLib.source_remove(self._paste_timeout)
            self._paste_timeout = None

    def _paste_selected(self, provider, shifted):
        self._paste_timeout = None
        if (self.paste_backend is not None and self.paste_backend.state == "ready"
                and self.clipboard.get_content() == provider
                and (self.window is None or not self.window.get_visible())):
            self.paste_backend.paste(self._on_pasted, shifted=shifted)
        return GLib.SOURCE_REMOVE

    def _on_pasted(self, success):
        if not success:
            self._set_status("The entry was copied, but automatic paste failed. Use Ctrl+V to paste it.")

    def _invalidate_read(self):
        self._read_version += 1
        if self._read_cancellable is not None:
            self._read_cancellable.cancel()
            self._read_cancellable = None

    @staticmethod
    def _has_text(clipboard):
        formats = clipboard.get_formats()
        return formats.contain_gtype(GObject.TYPE_STRING) or any(
            mime.startswith("text/") for mime in formats.get_mime_types())

    def on_clipboard_changed(self, clipboard):
        self._invalidate_read()
        if self._owned_provider is not None and clipboard.get_content() == self._owned_provider:
            return
        self._cancel_pending_paste()
        self._owned_provider = None
        self._current_item_id = None
        self._deleted_during_read.clear()
        formats = clipboard.get_formats()
        self._read_cancellable = Gio.Cancellable()
        if any(GObject.type_is_a(value_type, Gdk.Texture) for value_type in formats.get_gtypes()) or any(
                mime.startswith("image/") for mime in formats.get_mime_types()):
            clipboard.read_texture_async(self._read_cancellable, self.on_read_texture, self._read_version)
        elif self._has_text(clipboard):
            clipboard.read_text_async(self._read_cancellable, self.on_read_text, self._read_version)
        else:
            self._read_cancellable = None

    def on_read_text(self, clipboard, result, version):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            if version == self._read_version:
                self._read_cancellable = None
            logger.debug("Clipboard text is no longer available", exc_info=True)
            return
        if version != self._read_version:
            return
        self._read_cancellable = None
        assert self.history is not None
        try:
            if text and content_id("text", text.encode("utf-8")) in self._deleted_during_read:
                self._clear_clipboard()
                return
            item = self.history.add_text(text) if text else None
        except (OSError, UnicodeError):
            logger.exception("Cannot save clipboard text")
            return
        self._current_item_id = item.id if item else None
        self._refresh_visible_rows()

    def on_read_texture(self, clipboard, result, version):
        try:
            texture = clipboard.read_texture_finish(result)
        except GLib.Error:
            texture = None
        if version != self._read_version:
            return
        if texture is None:
            if self._has_text(clipboard):
                clipboard.read_text_async(self._read_cancellable, self.on_read_text, version)
            else:
                self._read_cancellable = None
            return
        self._read_cancellable = None
        assert self.history is not None
        try:
            png = texture.save_to_png_bytes().get_data()
            if content_id("image", png) in self._deleted_during_read:
                self._clear_clipboard()
                return
            item = self.history.add_image(png)
        except (OSError, GLib.Error):
            logger.exception("Cannot save clipboard image")
            return
        self._current_item_id = item.id if item else None
        self._refresh_visible_rows()
