# Polaroids

Paste photos onto your Cinnamon desktop as polaroids, then drag, resize and
rotate them. Each polaroid is its own transparent keep-below window, so it sits
above the wallpaper and desktop icons and under every normal app.

Runs on Python 3 + GTK 3 (PyGObject) plus libX11 via ctypes, all of which
Linux Mint ships. X11 only: the keep-below hints and the key grab do not exist
on Wayland.

## Install as a service

    ./install.sh

That copies `polaroids.service` into `~/.config/systemd/user/`, an autostart
entry into `~/.config/autostart/` that starts the service at login, and starts
it now. A tray icon (camera) appears with **Paste from clipboard**, **Open
images folder**, **Restart** and **Quit**. Quit stays quit until next login;
a crash is restarted by systemd after 3 seconds.

    journalctl --user -u polaroids -f     # logs
    systemctl --user restart polaroids    # same as the tray Restart
    ./uninstall.sh                        # remove service + autostart

## Pasting

Click the desktop and press **Ctrl+V**. If the clipboard holds an image (or a
copied image file) it becomes a polaroid under the pointer. If it holds
anything else the keypress is passed through to Nemo as normal, so pasting
files onto the desktop still works. Ctrl+V while a polaroid is focused does the
same thing.

The grab only applies while the desktop window has focus; Ctrl+V in every other
app is untouched.

Other ways in:

    ./polaroids.py paste              # clipboard image -> polaroid (e.g. from a hotkey)
    ./polaroids.py add pic.jpg ...    # files -> polaroids
    cp pic.png ~/.local/share/polaroids/inbox/   # anything dropped here is picked up

## Data

`~/.local/share/polaroids/` (override with `POLAROIDS_DIR`):

    inbox/      watched folder; files here become polaroids
    images/     the images in use
    trash/      removed images
    state.json  position, size and angle of every polaroid

## Controls

| Input              | Action        |
|--------------------|---------------|
| left-drag          | move          |
| scroll             | resize        |
| shift + scroll     | rotate        |
| `[` / `]`          | rotate        |
| `-` / `=`          | resize        |
| `0`                | straighten    |
| Ctrl+V             | paste another |
| Delete             | remove        |
| right-click        | menu          |

## Running without the service

    ./polaroids.py

SIGUSR1 restarts it in place, SIGTERM saves and exits. Set `POLAROIDS_DEBUG=1`
to log input events.

## Known limits

- Polaroids survive "Show desktop" because each is marked transient for nemo-desktop's window, which Muffin exempts. If nemo-desktop restarts they are re-parented automatically.
- Clicking a polaroid gives it keyboard focus like any other window.
- Nemo's own Ctrl+V on the desktop is bypassed only when the clipboard holds an image.
