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
  double-click        write on it (Enter finishes, Esc cancels)
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

import ctypes
import ctypes.util

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GdkPixbuf, GdkX11, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from x11grab import DesktopKeyGrab  # noqa: E402

DATA_DIR = Path(os.environ.get("POLAROIDS_DIR") or Path(GLib.get_user_data_dir()) / "polaroids")
INBOX = DATA_DIR / "inbox"
IMAGES = DATA_DIR / "images"
TRASH = DATA_DIR / "trash"
STATE = DATA_DIR / "state.json"
LOCK = DATA_DIR / "lock"

# Real Polaroid (SX-70 / 600) proportions: a 3.1" square image on a 3.5" x 4.2" card.
FRAME = 0.065       # side/top border as a fraction of photo width
BOTTOM = 0.29       # bottom border as a fraction of photo width
CORNER = 0.012      # card corner radius as a fraction of photo width
MARGIN = 16         # transparent margin around the frame for the drop shadow
MAX_SOURCE = 1600   # cap decoded image size (longest edge) to keep redraws cheap
DEFAULT_WIDTH = 280
MIN_WIDTH, MAX_WIDTH = 60, 2400
PAPER = (0.98, 0.97, 0.94)
INK = (0.12, 0.12, 0.24)              # dark blue marker
CAPTION_FONT = "Caveat, Permanent Marker, Z003, Comic Sans MS, cursive"
CAPTION_SIZE = 0.125                  # caption font size as a fraction of photo width
EMOJI_SCALE = 0.68                    # emoji come from a fallback font and dwarf the handwriting
FONT_DIR = Path(__file__).resolve().parent / "fonts"
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


def register_fonts():
    """Make the bundled handwriting font visible to Pango without installing it."""
    try:
        lib = ctypes.util.find_library("fontconfig")
        fc = ctypes.CDLL(lib)
        fc.FcConfigGetCurrent.restype = ctypes.c_void_p
        fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        for f in sorted(FONT_DIR.glob("*.[to]tf")):
            fc.FcConfigAppFontAddFile(fc.FcConfigGetCurrent(), str(f).encode())
    except Exception as e:
        log("bundled fonts unavailable:", e)


def is_emoji(ch):
    o = ord(ch)
    return (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
            or o in (0xFE0F, 0x200D, 0x20E3) or 0x1F3FB <= o <= 0x1F3FF)


def emoji_attrs(text, scale):
    """Pango attributes shrinking every emoji run in text by `scale`."""
    attrs = Pango.AttrList()
    pos = 0
    start = None
    for ch in text + "\x00":
        n = len(ch.encode("utf-8"))
        if is_emoji(ch):
            if start is None:
                start = pos
        elif start is not None:
            a = Pango.attr_scale_new(scale)
            a.start_index, a.end_index = start, pos
            attrs.insert(a)
            start = None
        pos += n
    return attrs


def rounded_rect(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


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
        self.caption = str(entry.get("caption", ""))
        self.square = bool(entry.get("square", True))
        self.pan = float(entry.get("pan", 0.5))        # 0 = keep top/left edge, 1 = bottom/right
        self.turn = int(entry.get("turn", 0)) % 360   # photo turned inside the frame, 90 deg steps
        self.editing = None            # caption text before editing began, while editing
        self.cursor = 0                # caret position (characters) while editing
        self.anchor = None             # other end of the selection, or None
        self.sel_drag = False          # mouse is dragging a selection
        self.band = None               # (x, y, w, h, layout, ox, oy) of the caption strip, frame coords
        self.source = load_pixbuf(IMAGES / self.image)
        self.update_crop()
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
        self.connect("focus-out-event", self.on_focus_out)
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
    def update_crop(self):
        """Turn the source, then pick the visible part: a square slid by `pan`, or everything."""
        rot = {0: GdkPixbuf.PixbufRotation.NONE, 90: GdkPixbuf.PixbufRotation.CLOCKWISE,
               180: GdkPixbuf.PixbufRotation.UPSIDEDOWN, 270: GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE}
        self.pixbuf = self.source.rotate_simple(rot.get(self.turn, GdkPixbuf.PixbufRotation.NONE))
        w, h = self.pixbuf.get_width(), self.pixbuf.get_height()
        if self.square:
            side = min(w, h)
            self.crop = (round((w - side) * self.pan), round((h - side) * self.pan), side, side)
        else:
            self.crop = (0, 0, w, h)
        self.pix_w, self.pix_h = self.crop[2], self.crop[3]

    def landscape(self):
        return self.pixbuf.get_width() > self.pixbuf.get_height()

    def set_fit(self, square=None, pan=None, turn=None):
        if square is not None:
            self.square = bool(square)
        if pan is not None:
            self.pan = max(0.0, min(1.0, pan))
        if turn is not None:
            self.turn = turn % 360
        self.update_crop()
        self.apply_geometry()

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
                "width": round(self.width, 1), "angle": round(self.angle, 2),
                "caption": self.caption, "square": self.square,
                "pan": round(self.pan, 3), "turn": self.turn}

    # ---- drawing --------------------------------------------------------
    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        fw, fh, b = self.frame_size()
        w = self.width
        r = w * CORNER
        cr.translate(self.cur_w / 2, self.cur_h / 2)
        cr.rotate(math.radians(self.angle))
        x0, y0 = -fw / 2, -fh / 2

        # soft drop shadow: stacked translucent rounded rects, offset down-right
        for i in range(10, 0, -1):
            cr.set_source_rgba(0, 0, 0, 0.03)
            rounded_rect(cr, x0 - i + 3, y0 - i + 6, fw + 2 * i, fh + 2 * i, r + i)
            cr.fill()

        # the card: warm white with a faint diagonal gradient, hairline edge
        paper = cairo.LinearGradient(x0, y0, x0 + fw, y0 + fh)
        paper.add_color_stop_rgb(0, *[c + 0.015 for c in PAPER])
        paper.add_color_stop_rgb(1, *[c - 0.03 for c in PAPER])
        rounded_rect(cr, x0, y0, fw, fh, r)
        cr.set_source(paper)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.10)
        cr.set_line_width(1)
        cr.stroke()

        # the photo, sunk into the card: dark rim, image, inner shadow, slight vignette
        px, py = x0 + b, y0 + b
        pw, ph = w, w * self.pix_h / self.pix_w
        cr.set_source_rgb(0.10, 0.09, 0.09)
        cr.rectangle(px - 1, py - 1, pw + 2, ph + 2)
        cr.fill()

        cr.save()
        cr.rectangle(px, py, pw, ph)
        cr.clip()
        cx0, cy0, cw, ch = self.crop
        scale = pw / cw
        cr.translate(px, py)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, -cx0, -cy0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()

        cr.save()
        cr.rectangle(px, py, pw, ph)
        cr.clip()
        inset = max(2.0, w * 0.02)
        top = cairo.LinearGradient(px, py, px, py + inset * 2)
        top.add_color_stop_rgba(0, 0, 0, 0, 0.35)
        top.add_color_stop_rgba(1, 0, 0, 0, 0)
        cr.set_source(top)
        cr.rectangle(px, py, pw, inset * 2)
        cr.fill()
        left = cairo.LinearGradient(px, py, px + inset, py)
        left.add_color_stop_rgba(0, 0, 0, 0, 0.18)
        left.add_color_stop_rgba(1, 0, 0, 0, 0)
        cr.set_source(left)
        cr.rectangle(px, py, inset, ph)
        cr.fill()
        vignette = cairo.RadialGradient(px + pw / 2, py + ph / 2, min(pw, ph) * 0.45,
                                        px + pw / 2, py + ph / 2, max(pw, ph) * 0.75)
        vignette.add_color_stop_rgba(0, 0, 0, 0, 0)
        vignette.add_color_stop_rgba(1, 0, 0, 0, 0.16)
        cr.set_source(vignette)
        cr.rectangle(px, py, pw, ph)
        cr.fill()
        cr.restore()

        self.draw_caption(cr, x0 + b, py + ph, w, fh - b - ph)
        return True

    def caption_layout(self, cr, width, height, text):
        """Lay out the caption at the normal size, shrinking it until it fits the strip."""
        layout = PangoCairo.create_layout(cr)
        font = Pango.FontDescription.from_string(CAPTION_FONT)
        layout.set_width(int(width * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_text(text, -1)
        layout.set_attributes(emoji_attrs(text, EMOJI_SCALE))
        size = max(8.0, self.width * CAPTION_SIZE)
        while True:
            font.set_absolute_size(size * Pango.SCALE)
            layout.set_font_description(font)
            _ink, logical = layout.get_pixel_extents()
            if logical.height <= height or size <= 4.0:
                return layout
            size = max(4.0, size * 0.9)

    def draw_caption(self, cr, x, y, width, height):
        """Handwriting in the bottom band; a caret while editing, a hint when empty."""
        editing = self.editing is not None
        text = self.caption
        if not text and not editing:
            return
        cr.save()
        cr.rectangle(x, y, width, height)
        cr.clip()
        pad = width * 0.04
        layout = self.caption_layout(cr, width - 2 * pad, height - 2 * pad, text or "write something\u2026")
        _ink, logical = layout.get_pixel_extents()
        ox, oy = x + pad, y + max(0.0, (height - logical.height) / 2) - logical.y
        self.band = (x, y, width, height, layout, ox, oy)
        sel = self.selection()
        if editing and sel and text:
            cr.set_source_rgba(0.45, 0.6, 0.95, 0.4)
            for rx, ry, rw, rh in self.selection_rects(layout, *sel):
                cr.rectangle(ox + rx, oy + ry, rw, rh)
            cr.fill()
        cr.move_to(ox, oy)
        if text:
            cr.set_source_rgba(*INK, 0.92)
        else:
            cr.set_source_rgba(0, 0, 0, 0.22)
        PangoCairo.show_layout(cr, layout)
        if editing:
            strong, _weak = layout.get_cursor_pos(self.byte_index(self.cursor))
            cx = ox + strong.x / Pango.SCALE
            cy = oy + strong.y / Pango.SCALE
            ch = strong.height / Pango.SCALE
            if not text:
                cx = x + width / 2 - logical.width / 2 - 2   # caret ahead of the hint
            cr.set_source_rgba(*INK, 0.9)
            cr.set_line_width(max(1.0, self.width * 0.005))
            cr.move_to(cx, cy)
            cr.line_to(cx, cy + ch)
            cr.stroke()
        cr.restore()

    def selection_rects(self, layout, start, end):
        """Pixel rectangles (layout coords) covering characters [start, end)."""
        bs, be = self.byte_index(start), self.byte_index(end)
        rects = []
        it = layout.get_iter()
        while True:
            line = it.get_line_readonly()
            ls, le = line.start_index, line.start_index + line.length
            _ink, lg = it.get_line_extents()
            a, b = max(bs, ls), min(be, le)
            if a < b or (a == b and ls <= bs < le and bs == be):
                x1 = lg.x if a <= ls else layout.index_to_pos(a).x
                x2 = lg.x + lg.width if b >= le else layout.index_to_pos(b).x
                rects.append((x1 / Pango.SCALE, lg.y / Pango.SCALE,
                              max(2.0, (x2 - x1) / Pango.SCALE), lg.height / Pango.SCALE))
            if not it.next_line():
                break
        return rects

    # ---- writing --------------------------------------------------------
    def start_edit(self):
        if self.editing is None:
            self.editing = self.caption
            self.cursor, self.anchor = len(self.caption), None
        if not self.is_active():
            self.present_with_time(Gtk.get_current_event_time())
        self.queue_draw()

    def on_focus_out(self, *_):
        debug("focus out", self.id)
        # Focus bounces briefly on click/present; only commit if it really went elsewhere.
        GLib.timeout_add(150, lambda: self.is_active() or self.finish_edit())
        return False

    def finish_edit(self, cancel=False):
        if self.editing is None:
            return False
        if cancel:
            self.caption = self.editing
        self.editing = None
        self.anchor = None
        self.sel_drag = False
        self.caption = self.caption.strip()
        self.cursor = len(self.caption)
        self.queue_draw()
        self.manager.save_soon()
        return False

    def set_caption(self, text, cursor=None):
        self.caption = text
        if cursor is not None:
            self.cursor = cursor
        self.cursor = max(0, min(len(text), self.cursor))
        self.queue_draw()
        self.manager.save_soon()

    # -- text model: positions are character indices, Pango wants bytes --
    def byte_index(self, i):
        return len(self.caption[:i].encode("utf-8"))

    def char_index(self, b):
        return len(self.caption.encode("utf-8")[:b].decode("utf-8", "ignore"))

    def selection(self):
        if self.anchor is None or self.anchor == self.cursor:
            return None
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def replace_selection(self, text):
        """Insert text at the caret, replacing any selection."""
        sel = self.selection() or (self.cursor, self.cursor)
        self.anchor = None
        self.set_caption(self.caption[:sel[0]] + text + self.caption[sel[1]:], sel[0] + len(text))

    def word_left(self, i):
        t = self.caption
        while i > 0 and not t[i - 1].isalnum():
            i -= 1
        while i > 0 and t[i - 1].isalnum():
            i -= 1
        return i

    def word_right(self, i):
        t = self.caption
        while i < len(t) and not t[i].isalnum():
            i += 1
        while i < len(t) and t[i].isalnum():
            i += 1
        return i

    def line_bounds(self, i):
        """Start and end (character indices) of the visual line holding position i."""
        layout = self.band[4] if self.band else None
        if layout is None or not self.caption:
            return 0, len(self.caption)
        line_no, _x = layout.index_to_line_x(self.byte_index(i), False)
        line = layout.get_line_readonly(line_no)
        end = line.start_index + line.length
        text = self.caption
        s, e = self.char_index(line.start_index), self.char_index(end)
        if e > s and e <= len(text) and text[e - 1] == "\n":
            e -= 1
        elif e < len(text) and text[e - 1:e] == " " and line_no < layout.get_line_count() - 1:
            e -= 1                     # wrapped lines own the trailing space; caret sits before it
        return s, e

    def line_move(self, i, direction):
        """Position on the line above (-1) or below (+1), keeping the caret's x."""
        layout = self.band[4] if self.band else None
        if layout is None or not self.caption:
            return i
        strong, _weak = layout.get_cursor_pos(self.byte_index(i))
        line_no, _x = layout.index_to_line_x(self.byte_index(i), False)
        target = line_no + direction
        if target < 0:
            return 0
        if target >= layout.get_line_count():
            return len(self.caption)
        it = layout.get_iter()
        for _ in range(target):
            it.next_line()
        _ink, lg = it.get_line_extents()
        _inside, b, trailing = layout.xy_to_index(strong.x, lg.y + lg.height // 2)
        return min(len(self.caption), self.char_index(b) + trailing)

    def move_cursor(self, pos, shift):
        pos = max(0, min(len(self.caption), pos))
        if shift:
            if self.anchor is None:
                self.anchor = self.cursor
        else:
            self.anchor = None
        self.cursor = pos
        self.queue_draw()

    def position_at(self, ev):
        """Character index under a pointer event, or None if it is not on the caption strip."""
        if self.band is None or not self.caption:
            return None
        a = -math.radians(self.angle)
        dx, dy = ev.x - self.cur_w / 2, ev.y - self.cur_h / 2
        fx, fy = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
        bx, by, bw, bh, layout, ox, oy = self.band
        if not (bx <= fx <= bx + bw and by <= fy <= by + bh):
            return None
        _inside, b, trailing = layout.xy_to_index(int((fx - ox) * Pango.SCALE), int((fy - oy) * Pango.SCALE))
        return min(len(self.caption), self.char_index(b) + trailing)

    def edit_key(self, ev, k, ctrl):
        """Key handling while writing. Returns True when the key was consumed."""
        shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
        sel = self.selection()
        c = self.cursor
        if k == "Escape":
            self.finish_edit(cancel=True)
        elif k in ("Return", "KP_Enter") and not shift:
            self.finish_edit()
        elif k in ("Return", "KP_Enter"):
            self.replace_selection("\n")
        elif k in ("Left", "KP_Left"):
            if sel and not shift:
                self.move_cursor(sel[0], False)
            else:
                self.move_cursor(self.word_left(c) if ctrl else c - 1, shift)
        elif k in ("Right", "KP_Right"):
            if sel and not shift:
                self.move_cursor(sel[1], False)
            else:
                self.move_cursor(self.word_right(c) if ctrl else c + 1, shift)
        elif k in ("Up", "KP_Up"):
            self.move_cursor(0 if ctrl else self.line_move(c, -1), shift)
        elif k in ("Down", "KP_Down"):
            self.move_cursor(len(self.caption) if ctrl else self.line_move(c, 1), shift)
        elif k in ("Home", "KP_Home"):
            self.move_cursor(0 if ctrl else self.line_bounds(c)[0], shift)
        elif k in ("End", "KP_End"):
            self.move_cursor(len(self.caption) if ctrl else self.line_bounds(c)[1], shift)
        elif k == "BackSpace":
            if sel:
                self.replace_selection("")
            elif c > 0:
                self.anchor = self.word_left(c) if ctrl else c - 1
                self.replace_selection("")
        elif k in ("Delete", "KP_Delete"):
            if sel:
                self.replace_selection("")
            elif c < len(self.caption):
                self.anchor = self.word_right(c) if ctrl else c + 1
                self.replace_selection("")
        elif ctrl and k in ("a", "A"):
            self.anchor, self.cursor = 0, len(self.caption)
            self.queue_draw()
        elif ctrl and k in ("c", "C", "x", "X"):
            if sel:
                self.manager.clipboard.set_text(self.caption[sel[0]:sel[1]], -1)
                if k in ("x", "X"):
                    self.replace_selection("")
        elif ctrl and k in ("v", "V"):
            text = self.manager.clipboard.wait_for_text()
            if text:
                self.replace_selection(text)
        elif not ctrl:
            ch = Gdk.keyval_to_unicode(ev.keyval)
            if ch and (chr(ch).isprintable() or ch == 0x20):
                self.replace_selection(chr(ch))
        return True

    # ---- interaction ----------------------------------------------------
    def on_press(self, _w, ev):
        debug("press", ev.button, ev.type, ev.x_root, ev.y_root)
        self.manager.raised(self)
        if ev.button == 1 and ev.type == Gdk.EventType._2BUTTON_PRESS:
            self.drag = None
            self.start_edit()
        elif ev.button == 1 and self.editing is not None and self.position_at(ev) is not None:
            shift = bool(ev.state & Gdk.ModifierType.SHIFT_MASK)
            self.move_cursor(self.position_at(ev), shift)
            if not shift:
                self.anchor = self.cursor
            self.sel_drag = True
        elif ev.button == 1:
            self.drag = (ev.x_root - self.x, ev.y_root - self.y)
        elif ev.button == 3:
            self.popup_menu(ev)
        return True

    def on_motion(self, _w, ev):
        if self.sel_drag:
            pos = self.position_at(ev)
            if pos is not None:
                self.move_cursor(pos, True)
        elif self.drag:
            self.x, self.y = int(ev.x_root - self.drag[0]), int(ev.y_root - self.drag[1])
            self.move(self.x, self.y)
        return True

    def on_release(self, _w, ev):
        if ev.button == 1 and self.sel_drag:
            self.sel_drag = False
            if self.anchor == self.cursor:
                self.anchor = None
        elif ev.button == 1 and self.drag:
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
        if self.editing is not None:
            return self.edit_key(ev, k, ctrl)
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
        elif k in ("Left", "Up") and self.square:
            self.set_fit(pan=self.pan - 0.05)
        elif k in ("Right", "Down") and self.square:
            self.set_fit(pan=self.pan + 0.05)
        elif k in ("Return", "KP_Enter", "F2"):
            self.start_edit()
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
            ("Write on it\u2026", lambda *_: self.start_edit()),
            ("Clear writing", lambda *_: self.set_caption("")),
            None,
            ("Fit", self.fit_menu_items()),
            None,
            ("Paste from clipboard", lambda *_: self.manager.paste_clipboard()),
            ("Remove", lambda *_: self.manager.remove(self)),
        ])
        menu.popup_at_pointer(ev)


    def fit_menu_items(self):
        """Radio items: whole image or square, which edge to keep, and how the photo is turned."""
        def radio(label, active, **fit):
            return (label, lambda mi: mi.get_active() and self.set_fit(**fit), active, "radio")

        sq, pan = self.square, self.pan
        if self.landscape():
            edges = (("Keep left", 0.0), ("Keep middle", 0.5), ("Keep right", 1.0))
        else:
            edges = (("Keep top", 0.0), ("Keep middle", 0.5), ("Keep bottom", 1.0))
        items = [radio("Whole image", not sq, square=False)]
        for label, p in edges:
            items.append(radio(label, sq and abs(pan - p) < 0.025, square=True, pan=p))
        if sq and all(abs(pan - p) >= 0.025 for _, p in edges):
            items.append(radio(f"Custom ({pan:.0%})", True, square=True))
        items.append(None)
        for label, t in (("Upright", 0), ("On its side (right)", 90),
                         ("Upside down", 180), ("On its side (left)", 270)):
            items.append(radio(label, self.turn == t, turn=t))
        items.append(None)
        items.append(("Arrow keys slide the crop", None))
        return items


def build_menu(items):
    menu = Gtk.Menu()
    for item in items:
        if item is None:
            menu.append(Gtk.SeparatorMenuItem())
            continue
        if isinstance(item[1], list):               # submenu
            mi = Gtk.MenuItem(label=item[0])
            mi.set_submenu(build_menu(item[1]))
        elif len(item) > 2:                         # check / radio item
            mi = Gtk.CheckMenuItem(label=item[0])
            mi.set_draw_as_radio(len(item) > 3)
            mi.set_active(bool(item[2]))
            mi.connect("toggled", item[1])
        elif item[1] is None:                       # plain hint
            mi = Gtk.MenuItem(label=item[0])
            mi.set_sensitive(False)
        else:
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
        GLib.idle_add(lambda: self.restack() or False)

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

    def raised(self, p):
        """Remember that p is now on top: state.json is kept in stacking order, bottom first."""
        if p.id in self.polaroids and next(reversed(self.polaroids)) != p.id:
            self.polaroids[p.id] = self.polaroids.pop(p.id)
            self.save_soon()

    def restack(self):
        """Raise every polaroid in saved order so the stack survives a restart."""
        for p in self.polaroids.values():
            win = p.get_window()
            if win is not None:
                win.raise_()

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
    register_fonts()
    manager = Manager()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGUSR1, lambda *_: manager.restart() or True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: manager.quit() or True)
    log("running, pid", os.getpid(), "watching", INBOX)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
