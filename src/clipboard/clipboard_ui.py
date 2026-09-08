import gi

from .screen_dimensions import calculate_window_size

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

class ClipboardWindow(Gtk.ApplicationWindow):
  def __init__(self, **kwargs):
    super().__init__(**kwargs, title='')

class Application(Gtk.Application):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, application_id='com.clipboard.history', **kwargs)
    self.window = None
    self.rows = []

  def clear_all(self, _widget):
    pass

  def do_activate(self):
    if not self.window:
      self.window = ClipboardWindow(application=self)
      self.window.set_resizable(False)
      self.window.set_default_size(*calculate_window_size(self.window.get_display()))

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

    self.window.present()
