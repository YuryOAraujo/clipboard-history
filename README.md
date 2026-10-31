# Clipboard History

A small GTK 4 clipboard history popup for Linux. Text and PNG images survive
closing the popup and restarting the application. Clicking an entry copies it,
closes the popup, and sends Ctrl+V after focus returns to the previous app.
Images paste into applications that accept images from the clipboard.
Hold Shift when selecting an entry to send Ctrl+Shift+V instead (for terminals
or pasting without formatting).

Automatic paste uses the standard
[XDG RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html).
On first opening the popup, GNOME asks for **keyboard control only**. Allow it
to enable automatic paste. No selection is pasted as a result of approving the
dialog; select an entry after approval. The app saves the portal's restore
token privately when available, so GNOME can remember permission across runs.
The portal must support the RemoteDesktop interface, including keyboard input;
this is available in the tested GNOME 50 Wayland session.

Use the **Auto-paste** toggle to turn this off or request permission again.
If access is denied or unavailable, selecting an entry still copies it for
manual pasting. Setting `CLIPBOARD_HISTORY_AUTO_PASTE=0` prevents automatic
permission requests on startup. GNOME's remote-control Stop button also turns
automatic paste off. This integration does not require GPaste, xdotool, a Shell
extension, or root access.

Requires Python 3.11+, GTK 4.8+, GdkPixbuf, and PyGObject. On Debian/Ubuntu,
install the system bindings with:

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-gdkpixbuf-2.0
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-deps -e .
```

Run `clipboard-history` (or `.venv/bin/clipboard-history`) to show/toggle the
popup. Bind that command to Super+V in your desktop's keyboard settings. Without
installing the package, run `PYTHONPATH=src .venv/bin/python -m clipboard` from
the repository root.

For login startup, use `clipboard-history --background` in an autostart entry.
The process keeps monitoring while the popup is hidden. Further launches talk
to the existing application, so they do not create independent histories.
The first normal launch opens the popup immediately.

On Wayland, the standard clipboard protocol only sends selection changes to
the focused client. Depending on the compositor, this GTK-only monitor can
therefore miss copies while hidden. Persistent storage cannot recover copies
the compositor never delivered. Complete background capture on those sessions
needs a supported clipboard service or compositor integration (such as the
planned GPaste work). See the [Wayland protocol documentation](https://wayland.freedesktop.org/docs/book/Protocol.html).

If you use the existing source-based desktop entries, keep the shortcut's
`Exec` command as it is and add `--background` to the `Exec` line in
`~/.config/autostart/com.clipboard.history.desktop`:

```ini
Exec=/absolute/path/to/clipboard-history/.venv/bin/python -m clipboard --background
Path=/absolute/path/to/clipboard-history/src
```

History is stored in `$XDG_DATA_HOME/clipboard-history`, defaulting to
`~/.local/share/clipboard-history`. The application saves metadata atomically
in `history.json` and full images in `clipboard-images/`. History has limits of
100 entries, 64 MiB of image files, and 4 MiB of UTF-8 text. A single item larger
than its budget is skipped; older entries are evicted as needed. Recopying an
existing entry moves it to the top without keeping duplicate data.

Only 160×100 image previews are held by visible rows. Hiding the popup releases
those rows; selecting an image loads its PNG bytes for the system clipboard.
Unused image files are deleted on startup, deletion, clearing, and eviction.
Images still referenced by saved history are retained on startup.

Deleting the current item also clears the system clipboard so it cannot return
on restart. Clear All clears both history and the system clipboard. The app
keeps an empty clipboard value owned until another application copies something,
which prevents a clipboard manager from restoring the last selection. History is
stored locally as readable text/PNG files with owner-only permissions; it is
not encrypted. Copies made while the application is stopped cannot be captured.

Run the regression tests with:

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The GTK window and process tests use a separate Broadway display and D-Bus
session, without touching the desktop clipboard. Run the full suite with
`gtk4-broadwayd` and `dbus-run-session` installed:

```sh
.venv/bin/python tests/run_gui_tests.py
```

After changing the source, restart the running application to load the new code.
Closing its popup only hides it; the background process continues running. An
instance from before persistence was added has RAM-only history, which will be
discarded on restart. Once persistence is active, saved history survives restarts.
