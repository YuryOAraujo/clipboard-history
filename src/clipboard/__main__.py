import sys

from .clipboard_ui import Application


def main():
  app = Application()
  return app.run(sys.argv)


if __name__ == '__main__':
  raise SystemExit(main())
