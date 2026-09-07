import gi
import sys

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

class ClipboardWindow(Gtk.ApplicationWindow):
  def __init__(self, **kwargs):
    super().__init__(**kwargs, title='Clipboard History')

class Application(Gtk.Application):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, application_id='com.clipboard.history', **kwargs)
    self.window = None

  def do_activate(self):
    if not self.window:
      self.window = ClipboardWindow(application=self)

    self.window.present()

if __name__ == '__main__':
  app = Application()
  app.run(sys.argv)
