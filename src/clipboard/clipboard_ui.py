import hashlib

import gi

from .screen_dimensions import calculate_window_size

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Gdk, Pango
from typing import Literal


class ClipboardWindow(Gtk.ApplicationWindow):
  def __init__(self, **kwargs):
    super().__init__(**kwargs, title='')

class ClipboardEntry(Gtk.ListBoxRow):
  def __init__(self, content: str | Gdk.Texture, type: Literal['text', 'image'], **kwargs):
      super().__init__(**kwargs)

      self.content = content
      self.type = type

      row = Gtk.Box(spacing=12, margin_start=8, margin_bottom=8, margin_top=8, margin_end=8, valign=Gtk.Align.START)
      
      if type == 'text' and isinstance(content, str):
        label = Gtk.Label(label=content, hexpand=True, xalign=0, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True)
        row.append(label)
      elif type == 'image' and isinstance(self.content, Gdk.Texture):
        preview = Gtk.Box(hexpand=True)

        picture = Gtk.Picture()
        picture.set_paintable(self.content)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        picture.set_size_request(160, 100)
        picture.set_halign(Gtk.Align.START)
        picture.set_valign(Gtk.Align.CENTER)

        preview.append(picture)
        row.append(preview)

      delete_button = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.START)
      delete_button.connect('clicked', self.on_delete)

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

  def create_window(self):
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
    self.hold()

    scrollable_panel = Gtk.ScrolledWindow(margin_bottom=6, margin_end=12, margin_start=12, margin_top=6)
    self.window.set_child(scrollable_panel)

    label = Gtk.Label(label='Clipboard', hexpand=True, xalign=0.10)
    clear_button = Gtk.Button(label='Clear All')
    clear_button.connect('clicked', self.clear_all)

    header_bar.pack_start(label)
    header_bar.pack_end(clear_button)
  
    scrollable_panel.set_child(self.rows)

    self.rows.connect('row-activated', self.handle_row_click)
    self.window.connect('close-request', self.handle_close_request)
    self.window.connect('notify::is-active', self.handle_active_change)

  def do_activate(self):
    if not self.window:
      self.create_window()
      assert self.window is not None
      self.window.present()
      return

    self.window.hide() if self.window.get_visible() else self.window.present()

  def handle_row_click(self, _listbox: Gtk.ListBox, row: ClipboardEntry):
    assert self.clipboard is not None
    self.ignore_next_change = True
    
    if row.type == 'text':
      self.clipboard.set(row.content)
    elif row.type =='image':
      assert isinstance(row.content, Gdk.Texture)
      gbytes = row.content.save_to_png_bytes()
      content = Gdk.ContentProvider.new_for_bytes("image/png", gbytes)
      self.clipboard.set_content(content)

  def handle_close_request(self, _widget):
    assert self.window is not None
    self.window.hide()

    return True

  def handle_active_change(self, window: Gtk.Window, _param):
    if not window.is_active():
      window.hide()

  def on_clipboard_changed(self, _clipboard):
    if self.ignore_next_change:
      self.ignore_next_change = False
      return

    assert self.clipboard is not None
    formats = self.clipboard.get_formats().get_mime_types()

    if formats and any(mime.startswith('image/') for mime in formats):
      self.clipboard.read_texture_async(None, self.on_read_texture)
    elif formats and any(mime.startswith('text/') for mime in formats):
      self.clipboard.read_text_async(None, self.on_read_text)      

  def on_read_text(self, _clipboard, result):
    assert self.clipboard is not None
    assert self.rows is not None

    text = self.clipboard.read_text_finish(result)
    
    if text is None:
      return

    first = self.rows.get_first_child()
    if isinstance(first, ClipboardEntry) and first.type == 'text' and text == first.content:
      return

    self.rows.prepend(ClipboardEntry(content=text, type='text'))

  def on_read_texture(self, _clipboard, result):
    assert self.clipboard is not None
    assert self.rows is not None

    texture = self.clipboard.read_texture_finish(result)

    if texture is None:
      return
    
    first = self.rows.get_first_child()
    if isinstance(first, ClipboardEntry) and first.type == 'image' and isinstance(first.content, Gdk.Texture) and self.texture_hash(first.content) == self.texture_hash(texture):
        return

    self.rows.prepend(ClipboardEntry(content=texture, type='image'))

  def texture_hash(self, texture: Gdk.Texture):
    data = texture.save_to_png_bytes().get_data()
    assert data is not None
    return hashlib.sha256(data).digest()
