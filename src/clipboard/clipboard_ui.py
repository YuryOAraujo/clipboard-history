import gi

from .screen_dimensions import calculate_window_size

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Pango

class ClipboardWindow(Gtk.ApplicationWindow):
  def __init__(self, **kwargs):
    super().__init__(**kwargs, title='')

class ClipboardEntry(Gtk.ListBoxRow):
  def __init__(self, content = '', type = '', **kwargs):
      super().__init__(**kwargs)

      self.content = content
      self.type = type

      row = Gtk.Box(spacing=12, margin_start=8)
      label = Gtk.Label(label=content, hexpand=True, xalign=0, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True)

      delete_button = Gtk.Button(icon_name='user-trash-symbolic')
      delete_button.connect('clicked', self.on_delete)

      row.append(label)
      row.append(delete_button)

      self.set_child(row)

  def on_delete(self, _widget):
    listbox = self.get_parent()

    if isinstance(listbox, Gtk.ListBox):
      listbox.remove(self)
      

class Application(Gtk.Application):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, application_id='com.clipboard.history', **kwargs)
    self.window = None
    self.rows = None
    self.clipboard = None
    self.ignore_next_change = False

  def clear_all(self, _widget):
    assert self.rows is not None
    self.rows.remove_all()

  def do_activate(self):
    if not self.window:
      self.window = ClipboardWindow(application=self)
      self.window.set_resizable(False)
      self.window.set_default_size(*calculate_window_size(self.window.get_display()))
      self.rows = Gtk.ListBox()
      display = Gdk.Display.get_default()

      assert display is not None
      self.clipboard = display.get_clipboard()
      self.clipboard.connect('changed', self.on_clipboard_changed)

      header_bar = Gtk.HeaderBar()
      header_bar.set_decoration_layout("icon:close")
      
      self.window.set_titlebar(header_bar)

      scrollable_panel = Gtk.ScrolledWindow(margin_bottom=6, margin_end=12, margin_start=12, margin_top=6)
      self.window.set_child(scrollable_panel)

      label = Gtk.Label(label='Clipboard', hexpand=True, xalign=0.10)
      clear_button = Gtk.Button(label='Clear All')
      clear_button.connect('clicked', self.clear_all)

      header_bar.pack_start(label)
      header_bar.pack_end(clear_button)
    
      scrollable_panel.set_child(self.rows)

      self.rows.connect('row-activated', self.handle_row_click)

    self.window.present()

  def handle_row_click(self, _listbox: Gtk.ListBox, row: ClipboardEntry):
    assert self.clipboard is not None
    self.ignore_next_change = True
    self.clipboard.set(row.content)

  def on_clipboard_changed(self, _clipboard):
    if self.ignore_next_change:
      self.ignore_next_change = False
      return

    assert self.clipboard is not None
    formats = self.clipboard.get_formats().get_mime_types()

    if formats and any(mime.startswith('image/') for mime in formats):
      pass
    elif formats and any(mime.startswith('text/') for mime in formats):
      self.clipboard.read_text_async(None, self.on_read_text)      

  def on_read_text(self, _clipboard, result):
    assert self.clipboard is not None
    assert self.rows is not None

    text = self.clipboard.read_text_finish(result)

    if text is None:
      return

    self.rows.prepend(ClipboardEntry(content=text, type='text'))

