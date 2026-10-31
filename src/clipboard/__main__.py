import logging
import sys

from .clipboard_ui import Application


def main():
  logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
  app = Application()
  return app.run(sys.argv)


if __name__ == '__main__':
  raise SystemExit(main())
