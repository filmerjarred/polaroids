#!/usr/bin/env python3
"""Polaroids: paste photos onto the desktop, then drag, resize and rotate them.

Each polaroid is its own undecorated, transparent, keep-below window. Ctrl+V
with the desktop focused (or over a polaroid) pastes the clipboard image. The
app also watches an inbox folder; anything dropped there becomes a polaroid
under the mouse pointer. State (position, size, angle) lives in a JSON file.

Usage:
  polaroids.py              run the app (single instance, with tray icon)
  polaroids.py add FILE...  queue image files as new polaroids
  polaroids.py paste        queue the clipboard image as a new polaroid

Controls on a polaroid:
  left-drag           move
  scroll              resize
  shift + scroll      rotate
  [ / ]  or  - / =    rotate / resize from the keyboard
  Ctrl+V              paste another polaroid
  Delete              remove
  right-click         menu

Signals: SIGUSR1 restarts the app in place, SIGTERM/SIGINT save and exit.
"""
import fcntl
import json
import math
import os
import random
import shutil
import signal
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GdkX11", "3.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from x11grab import DesktopKeyGrab  # noqa: E402

DATA_DIR = Path(os.environ.get("POLAROIDS_DIR") or Path(GLib.get_user_data_dir()) / "polaroids")
INBOX = DATA_DIR / "inbox"
IMAGES = DATA_DIR / "images"
TRASH = DATA_DIR / "trash"
STATE = DATA_DIR / "state.json"
LOCK = DATA_DIR / "lock"

FRAME = 0.06        # side/top border as a fraction of photo width
BOTTOM = 0.22       # bottom border as a fraction of photo width
MARGIN = 14         # transparent margin around the frame for the drop shadow
MAX_SOURCE = 1600   # cap decoded image size (longest edge) to keep redraws cheap
DEFAULT_WIDTH = 280
MIN_WIDTH, MAX_WIDTH = 60, 2400
PAPER = (0.985, 0.975, 0.945)
IMAGE_TARGETS = ("image/png", "image/jpeg", "image/bmp", "image/tiff", "image/webp", "image/gif")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")

DEBUG = bool(os.environ.get("POLAROIDS_DEBUG"))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def debug(*a):
    if DEBUG:
        log("debug:", *a)


def load_pixbuf(path):
    pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
    return shrink_pixbuf(pb.apply_embedded_orientation() or pb)


def shrink_pixbuf(pb):
    w, h = pb.get_width(), pb.get_height()
    longest = max(w, h)
    if longest > MAX_SOURCE:
        s = MAX_SOURCE / longest
        pb = pb.scale_simple(max(1, int(w * s)), max(1, int(h * s)), GdkPixbuf.InterpType.BILINEAR)
    return pb


def polygon_region(points):
    """Scanline-rasterise a convex polygon into a cairo.Region (for input shapes)."""
    region = cairo.Region()
    ys = [p[1] for p in points]
    y0, y1 = int(math.floor(min(ys))), int(math.ceil(max(ys)))
    n = len(points)
    for y in range(y0, y1 + 1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            (ax, ay), (bx, by) = points[i], points[(i + 1) % n]
            if (ay <= yc < by) or (by <= yc < ay):
                xs.append(ax + (yc - ay) * (bx - ax) / (by - ay))
        if len(xs) >= 2:
            xa, xb = int(math.floor(min(xs))), int(math.ceil(max(xs)))
            region.union(cairo.RectangleInt(xa, y, max(1, xb - xa), 1))
    return region


# ---------------------------------------------------------------------------
# clipboard helpers (shared by the app and the `paste` command)
# ---------------------------------------------------------------------------
def clipboard_targets(clipboard):
    ok, atoms = clipboard.wait_for_targets()
    return [a.name() for a in atoms] if ok else []


def clipboard_has_image(clipboard):
    targets = clipboard_targets(clipboard)
    if any(t in IMAGE_TARGETS for t in targets):
        return True
    return "text/uri-list" in targets and clipboard_image_uri(clipboard) is not None


def clipboard_image_uri(clipboard):
    for uri in clipboard.wait_for_uris() or []:
        if uri.startswith("file://"):
            p = Path(urllib.parse.unquote(uri[7:]))
            if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file():
                return p
    return None


def clipboard_pixbuf(clipboard):
    """Return a pixbuf from the clipboard: raw image data, or an image file URI."""
    targets = clipboard_targets(clipboard)
    if any(t in IMAGE_TARGETS for t in targets):
        pb = clipboard.wait_for_image()
        if pb is not None:
            return shrink_pixbuf(pb)
    if "text/uri-list" in targets:
        p = clipboard_image_uri(clipboard)
        if p is not None:
            return load_pixbuf(p)
    return None


class Polaroid(Gtk.Window):
    def __init__(self, manager, entry):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.manager = manager
        self.id = entry["id"]
        self.image = entry["image"]
        self.width = float(entry.get("width", DEFAULT_WIDTH))
        self.angle = float(entry.get("angle", 0.0))
        self.x = int(entry.get("x", 100))
        self.y = int(entry.get("y", 100))
        self.pixbuf = load_pixbuf(IMAGES / self.image)
        self.pix_w, self.pix_h = self.pixbuf.get_width(), self.pixbuf.get_height()
        self.drag = None
        self.cur_w = self.cur_h = 0
        self.desktop_parent = None

        self.set_title(f"Polaroid {self.id}")
        self.set_decorated(False)
        self.set_resizable(False)          # window tracks the drawing area's size request
        self.set_app_paintable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_below(True)
        self.stick()
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = bool(visual) and screen.is_composited()
        if visual:
            self.set_visual(visual)

        self.area = Gtk.DrawingArea()
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
        )
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("scroll-event", self.on_scroll)
        self.connect("key-press-event", self.on_key)
        self.connect("configure-event", self.on_configure)
        self.connect("realize", lambda *_: self.update_shape())
        self.add(self.area)

        self.apply_geometry(keep_centre=False)
        self.move(self.x, self.y)
        self.show_all()
        self.set_desktop_parent(manager.desktop_window)

    def set_desktop_parent(self, xid):
        """Mark this window transient for the desktop window (nemo-desktop).

        Muffin exempts windows transient for a desktop window from Show Desktop
        and always stacks them above their parent.
        """
        gdk_win = self.get_window()
        if gdk_win is None or not xid:
            return
        try:
            self.desktop_parent = GdkX11.X11Window.foreign_new_for_display(self.get_display(), xid)
            gdk_win.set_transient_for(self.desktop_parent)
        except Exception as e:
            log("could not set desktop parent:", e)

    # ---- geometry -------------------------------------------------------
    def frame_size(self):
        w = self.width
        h = w * self.pix_h / self.pix_w
        b = w * FRAME
        return w + 2 * b, h + b + w * BOTTOM, b

    def window_size(self):
        fw, fh, _ = self.frame_size()
        a = math.radians(self.angle)
        bw = abs(fw * math.cos(a)) + abs(fh * math.sin(a))
        bh = abs(fw * math.sin(a)) + abs(fh * math.cos(a))
        return int(math.ceil(bw)) + 2 * MARGIN, int(math.ceil(bh)) + 2 * MARGIN

    def frame_corners(self):
        """Corners of the rotated frame in window pixel coordinates."""
        fw, fh, _ = self.frame_size()
        a = math.radians(self.angle)
        ca, sa = math.cos(a), math.sin(a)
        cx, cy = self.cur_w / 2, self.cur_h / 2
        pts = []
        for px, py in ((-fw / 2, -fh / 2), (fw / 2, -fh / 2), (fw / 2, fh / 2), (-fw / 2, fh / 2)):
            pts.append((cx + px * ca - py * sa, cy + px * sa + py * ca))
        return pts

    def apply_geometry(self, keep_centre=True):
        new_w, new_h = self.window_size()
        if keep_centre and self.cur_w:
            cx, cy = self.x + self.cur_w / 2, self.y + self.cur_h / 2
            self.x, self.y = round(cx - new_w / 2), round(cy - new_h / 2)
            self.move(self.x, self.y)
        self.cur_w, self.cur_h = new_w, new_h
        self.area.set_size_request(new_w, new_h)
        self.update_shape()
        self.queue_draw()
        self.manager.save_soon()

    def update_shape(self):
        gdk_win = self.get_window()
        if gdk_win is None:
            return
        region = polygon_region(self.frame_corners())
        gdk_win.input_shape_combine_region(region, 0, 0)
        if not self.composited:
            gdk_win.shape_combine_region(region, 0, 0)   # no compositor: hard-clip the corners

    def set_width(self, w):
        self.width = max(MIN_WIDTH, min(MAX_WIDTH, w))
        self.apply_geometry()

    def set_angle(self, a):
        self.angle = ((a + 180) % 360) - 180
        self.apply_geometry()

    def to_entry(self):
        return {"id": self.id, "image": self.image, "x": self.x, "y": self.y,
                "width": round(self.width, 1), "angle": round(self.angle, 2)}

    # ---- drawing --------------------------------------------------------
    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        fw, fh, b = self.frame_size()
        cr.translate(self.cur_w / 2, self.cur_h / 2)
        cr.rotate(math.radians(self.angle))

        # soft drop shadow: stacked translucent rects, offset down-right
        for i in range(8, 0, -1):
            cr.set_source_rgba(0, 0, 0, 0.035)
            cr.rectangle(-fw / 2 - i + 3, -fh / 2 - i + 5, fw + 2 * i, fh + 2 * i)
            cr.fill()

        cr.set_source_rgb(*PAPER)
        cr.rectangle(-fw / 2, -fh / 2, fw, fh)
        cr.fill()

        cr.save()
        cr.translate(-fw / 2 + b, -fh / 2 + b)
        scale = self.width / self.pix_w
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.rectangle(0, 0, self.pix_w, self.pix_h)
        cr.fill()
        cr.restore()
        return True

    # ---- interaction ----------------------------------------------------
    def on_press(self, _w, ev):
        debug("press", ev.button, ev.x_root, ev.y_root)
        if ev.button == 1:
            self.drag = (ev.x_root - self.x, ev.y_root - self.y)
        elif ev.button == 3:
            self.popup_menu(ev)
        return True

    def on_motion(self, _w, ev):
        if self.drag:
            self.x, self.y = int(ev.x_root - self.drag[0]), int(ev.y_root - self.drag[1])
            self.move(self.x, self.y)
        return True

    def on_release(self, _w, ev):
        if ev.button == 1 and self.drag:
            self.drag = None
            self.manager.save_soon()
        return True

    def on_scroll(self, _w, ev):
        debug("scroll", ev.direction, ev.state)
        up = ev.direction == Gdk.ScrollDirection.UP
        down = ev.direction == Gdk.ScrollDirection.DOWN
        if not (up or down):
            return True
        if ev.state & Gdk.ModifierType.SHIFT_MASK:
            self.set_angle(self.angle + (3 if down else -3))
        else:
            self.set_width(self.width * (1.06 if up else 1 / 1.06))
        return True

    def on_key(self, _w, ev):
        k = Gdk.keyval_name(ev.keyval)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        debug("key", k, "ctrl" if ctrl else "")
        if ctrl and k in ("v", "V"):
            self.manager.paste_clipboard()
        elif k == "Delete":
            self.manager.remove(self)
        elif k == "bracketleft":
            self.set_angle(self.angle - 3)
        elif k == "bracketright":
            self.set_angle(self.angle + 3)
        elif k in ("equal", "plus"):
            self.set_width(self.width * 1.06)
        elif k == "minus":
            self.set_width(self.width / 1.06)
        elif k == "0":
            self.set_angle(0)
        else:
            return False
        return True

    def on_configure(self, _w, ev):
        if not self.drag:
            x, y = self.get_position()
            if (x, y) != (self.x, self.y):
                self.x, self.y = x, y
                self.manager.save_soon()
        return False

    def popup_menu(self, ev):
        menu = build_menu([
            ("Rotate left", lambda *_: self.set_angle(self.angle - 5)),
            ("Rotate right", lambda *_: self.set_angle(self.angle + 5)),
            ("Straighten", lambda *_: self.set_angle(0)),
            ("Bigger", lambda *_: self.set_width(self.width * 1.25)),
            ("Smaller", lambda *_: self.set_width(self.width / 1.25)),
            None,
            ("Paste from clipboard", lambda *_: self.manager.paste_clipboard()),
            ("Remove", lambda *_: self.manager.remove(self)),
        ])
        menu.popup_at_pointer(ev)


def build_menu(items):
    menu = Gtk.Menu()
    for item in items:
        if item is None:
            menu.append(Gtk.SeparatorMenuItem())
            continue
        mi = Gtk.MenuItem(label=item[0])
        mi.connect("activate", item[1])
        menu.append(mi)
    menu.show_all()
    return menu


class Tray:
    """Status icon with Paste / Restart / Quit. XApp on Cinnamon, Gtk.StatusIcon elsewhere."""

    def __init__(self, manager):
        self.menu = build_menu([
            ("Paste from clipboard", lambda *_: manager.paste_clipboard()),
            ("Open images folder", lambda *_: Gio.AppInfo.launch_default_for_uri(IMAGES.as_uri(), None)),
            None,
            ("Restart", lambda *_: manager.restart()),
            ("Quit", lambda *_: manager.quit()),
        ])
        self.icon = None
        try:
            gi.require_version("XApp", "1.0")
            from gi.repository import XApp
            icon = XApp.StatusIcon()
            icon.set_name("polaroids")
            icon.set_icon_name("camera-photo-symbolic")
            icon.set_tooltip_text("Polaroids")
            icon.set_primary_menu(self.menu)
            icon.set_secondary_menu(self.menu)
            self.icon = icon
            log("tray: xapp status icon")
        except Exception as e:
            debug("XApp unavailable:", e)
            icon = Gtk.StatusIcon.new_from_icon_name("camera-photo")
            icon.set_tooltip_text("Polaroids")
            icon.connect("popup-menu", self._popup)
            icon.connect("activate", lambda i: self._popup(i, 1, Gtk.get_current_event_time()))
            self.icon = icon
            log("tray: gtk status icon")

    def _popup(self, icon, button, t):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)


class Manager:
    def __init__(self):
        for d in (INBOX, IMAGES, TRASH):
            d.mkdir(parents=True, exist_ok=True)
        self.polaroids = {}
        self.save_pending = None
        self.pending_inbox = {}
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.desktop_window = None
        self.load_state()
        self.watch_inbox()
        for p in sorted(INBOX.iterdir()):
            self.schedule_ingest(p)
        self.tray = Tray(self)
        self.grab = None
        try:
            self.grab = DesktopKeyGrab(self.on_desktop_paste_key, log=log,
                                       on_change=self.on_desktop_windows_changed)
        except Exception as e:
            log("desktop Ctrl+V unavailable:", e)

    # ---- state ----------------------------------------------------------
    def load_state(self):
        if not STATE.exists():
            return
        try:
            entries = json.loads(STATE.read_text())
        except Exception as e:
            log("state.json unreadable, starting empty:", e)
            return
        for entry in entries:
            if not (IMAGES / entry.get("image", "")).exists():
                log("skipping entry with missing image:", entry)
                continue
            try:
                self.polaroids[entry["id"]] = Polaroid(self, entry)
            except Exception as e:
                log("could not restore", entry.get("image"), e)
        log(f"restored {len(self.polaroids)} polaroid(s)")

    def save_soon(self):
        if self.save_pending is None:
            self.save_pending = GLib.timeout_add(300, self.save)

    def save(self):
        self.save_pending = None
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([p.to_entry() for p in self.polaroids.values()], indent=1))
        tmp.replace(STATE)
        return False

    # ---- creating polaroids --------------------------------------------
    def create(self, image_name):
        pid = Path(image_name).stem
        px, py = self.pointer_position()
        debug("pointer at", px, py)
        entry = {"id": pid, "image": image_name, "width": DEFAULT_WIDTH,
                 "angle": random.uniform(-7, 7), "x": px, "y": py}
        p = Polaroid(self, entry)
        p.x = px - p.cur_w // 2 + random.randint(-20, 20)
        p.y = py - p.cur_h // 2 + random.randint(-20, 20)
        p.move(p.x, p.y)
        self.polaroids[pid] = p
        log("added", image_name, "at", p.x, p.y)
        self.save_soon()
        return p

    def add_pixbuf(self, pixbuf):
        name = f"{uuid.uuid4().hex[:8]}.png"
        pixbuf.savev(str(IMAGES / name), "png", [], [])
        return self.create(name)

    def paste_clipboard(self):
        try:
            pb = clipboard_pixbuf(self.clipboard)
        except Exception as e:
            log("clipboard read failed:", e)
            return False
        if pb is None:
            log("clipboard has no image")
            return False
        self.add_pixbuf(pb)
        return True

    def on_desktop_windows_changed(self, xids):
        self.desktop_window = xids[0] if xids else None
        log("desktop window for stacking:", hex(self.desktop_window) if self.desktop_window else None)
        for p in self.polaroids.values():
            p.set_desktop_parent(self.desktop_window)

    def on_desktop_paste_key(self):
        """Ctrl+V pressed with the desktop focused. True = we take it, False = let Nemo have it."""
        if not clipboard_has_image(self.clipboard):
            debug("desktop Ctrl+V: no image, replaying to desktop")
            return False
        GLib.idle_add(self._paste_once)
        return True

    def _paste_once(self):
        self.paste_clipboard()
        return False   # one-shot idle callback

    # ---- inbox ----------------------------------------------------------
    def watch_inbox(self):
        self.monitor = Gio.File.new_for_path(str(INBOX)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self.on_inbox_changed)

    def on_inbox_changed(self, _m, f, other, event):
        if event == Gio.FileMonitorEvent.RENAMED and other is not None:
            f = other
        if event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                     Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.RENAMED):
            self.schedule_ingest(Path(f.get_path()))

    def schedule_ingest(self, path, attempt=0):
        if path.name.startswith(".") or not path.is_file():
            return
        if path in self.pending_inbox:
            return
        self.pending_inbox[path] = GLib.timeout_add(150, self.ingest, path, attempt)

    def ingest(self, path, attempt):
        self.pending_inbox.pop(path, None)
        if not path.exists():
            return False
        try:
            load_pixbuf(path)   # validate before we commit to it
        except Exception as e:
            if attempt < 10:
                self.pending_inbox[path] = GLib.timeout_add(300, self.ingest, path, attempt + 1)
            else:
                log("giving up on", path, e)
                path.rename(TRASH / path.name)
            return False
        dest = IMAGES / f"{uuid.uuid4().hex[:8]}{path.suffix.lower() or '.img'}"
        shutil.move(str(path), str(dest))
        self.create(dest.name)
        return False

    def pointer_position(self):
        try:
            _screen, x, y = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
            return x, y
        except Exception:
            return 300, 300

    def remove(self, p):
        self.polaroids.pop(p.id, None)
        p.destroy()
        src = IMAGES / p.image
        if src.exists():
            shutil.move(str(src), str(TRASH / p.image))
        log("removed", p.image)
        self.save_soon()

    # ---- lifecycle ------------------------------------------------------
    def restart(self):
        log("restarting")
        self.save()
        os.execv(sys.executable, [sys.executable] + sys.argv)   # lock fd is close-on-exec

    def quit(self):
        log("quitting")
        self.save()
        Gtk.main_quit()


def cmd_add(files):
    INBOX.mkdir(parents=True, exist_ok=True)
    for f in files:
        src = Path(f)
        if not src.is_file():
            print("not a file:", src, file=sys.stderr)
            continue
        dest = INBOX / f"add-{int(time.time() * 1000)}-{src.name}"
        shutil.copy2(src, dest)
        print("queued", dest)


def cmd_paste():
    INBOX.mkdir(parents=True, exist_ok=True)
    pb = clipboard_pixbuf(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD))
    if pb is None:
        print("clipboard has no image", file=sys.stderr)
        return 1
    tmp = INBOX / f".paste-{int(time.time() * 1000)}.png"
    pb.savev(str(tmp), "png", [], [])
    tmp.rename(INBOX / tmp.name[1:])
    print("queued", INBOX / tmp.name[1:])
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(argv) > 1 and argv[1] == "add":
        cmd_add(argv[2:])
        return 0
    if len(argv) > 1 and argv[1] == "paste":
        return cmd_paste()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("polaroids is already running", file=sys.stderr)
        return 0

    Gdk.set_program_class("Polaroids")
    manager = Manager()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, lambda *_: manager.restart() or True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: manager.quit() or True)
    log("running, pid", os.getpid(), "watching", INBOX)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
