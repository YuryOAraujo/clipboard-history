import gi
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk


def calculate_window_size(display: Gdk.Display) -> tuple[int, int]:
  monitors = display.get_monitors()
  monitor = monitors.get_item(0)

  if monitor is None:
    return 576, 432

  screen_width = monitor.get_geometry().width
  screen_height = monitor.get_geometry().height

  return round(min(screen_width * 0.30, 576)), round(min(screen_height * 0.4, 432))
