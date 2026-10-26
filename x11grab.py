"""Grab a key combination only while a desktop-type window has keyboard focus.

Uses a passive XGrabKey on each _NET_WM_WINDOW_TYPE_DESKTOP window (nemo-desktop
on Cinnamon). X only activates such a grab when that window, or a descendant,
holds the focus, so the shortcut keeps working normally in every other app.

The keyboard grab is synchronous: the handler decides per press whether to
swallow the key (AsyncKeyboard) or replay it to the desktop as if we were never
there (ReplayKeyboard). That lets Nemo keep its own Ctrl+V for pasting files
when the clipboard holds no image.

Pure ctypes against libX11 on its own connection; integrates with the GLib loop
through the connection's file descriptor. No python-xlib dependency.
"""
import ctypes
import ctypes.util
import os

from gi.repository import GLib

_lib = ctypes.util.find_library("X11")
X = ctypes.CDLL(_lib) if _lib else None

c = ctypes
Display_p = c.c_void_p
Window = c.c_ulong
Atom = c.c_ulong
Bool = c.c_int

KeyPress = 2
PropertyNotify = 28
GrabModeSync, GrabModeAsync = 0, 1
AsyncKeyboard, ReplayKeyboard = 3, 5
ControlMask, LockMask, Mod2Mask = 1 << 2, 1 << 1, 1 << 4
PropertyChangeMask = 1 << 22
AnyPropertyType = 0


class XKeyEvent(c.Structure):
    _fields_ = [("type", c.c_int), ("serial", c.c_ulong), ("send_event", Bool),
                ("display", Display_p), ("window", Window), ("root", Window),
                ("subwindow", Window), ("time", c.c_ulong),
                ("x", c.c_int), ("y", c.c_int), ("x_root", c.c_int), ("y_root", c.c_int),
                ("state", c.c_uint), ("keycode", c.c_uint), ("same_screen", Bool)]


class XPropertyEvent(c.Structure):
    _fields_ = [("type", c.c_int), ("serial", c.c_ulong), ("send_event", Bool),
                ("display", Display_p), ("window", Window), ("atom", Atom),
                ("time", c.c_ulong), ("state", c.c_int)]


class XEvent(c.Union):
    _fields_ = [("type", c.c_int), ("xkey", XKeyEvent), ("xproperty", XPropertyEvent),
                ("pad", c.c_long * 24)]


if X is not None:
    X.XOpenDisplay.restype, X.XOpenDisplay.argtypes = Display_p, [c.c_char_p]
    X.XDefaultRootWindow.restype, X.XDefaultRootWindow.argtypes = Window, [Display_p]
    X.XInternAtom.restype, X.XInternAtom.argtypes = Atom, [Display_p, c.c_char_p, Bool]
    X.XGetWindowProperty.restype = c.c_int
    X.XGetWindowProperty.argtypes = [Display_p, Window, Atom, c.c_long, c.c_long, Bool, Atom,
                                     c.POINTER(Atom), c.POINTER(c.c_int), c.POINTER(c.c_ulong),
                                     c.POINTER(c.c_ulong), c.POINTER(c.POINTER(c.c_ubyte))]
    X.XQueryTree.restype = c.c_int
    X.XQueryTree.argtypes = [Display_p, Window, c.POINTER(Window), c.POINTER(Window),
                             c.POINTER(c.POINTER(Window)), c.POINTER(c.c_uint)]
    X.XFree.argtypes = [c.c_void_p]
    X.XGrabKey.argtypes = [Display_p, c.c_int, c.c_uint, Window, Bool, c.c_int, c.c_int]
    X.XUngrabKey.argtypes = [Display_p, c.c_int, c.c_uint, Window]
    X.XKeysymToKeycode.restype, X.XKeysymToKeycode.argtypes = c.c_ubyte, [Display_p, c.c_ulong]
    X.XStringToKeysym.restype, X.XStringToKeysym.argtypes = c.c_ulong, [c.c_char_p]
    X.XAllowEvents.argtypes = [Display_p, c.c_int, c.c_ulong]
    X.XPending.restype, X.XPending.argtypes = c.c_int, [Display_p]
    X.XNextEvent.argtypes = [Display_p, c.POINTER(XEvent)]
    X.XSync.argtypes = [Display_p, Bool]
    X.XFlush.argtypes = [Display_p]
    X.XSelectInput.argtypes = [Display_p, Window, c.c_long]
    X.XConnectionNumber.restype, X.XConnectionNumber.argtypes = c.c_int, [Display_p]
    _ErrorHandler = c.CFUNCTYPE(c.c_int, Display_p, c.c_void_p)
    X.XSetErrorHandler.argtypes = [_ErrorHandler]


def _ignore_errors(_dpy, _ev):
    return 0   # a grabbed desktop window may vanish between scans; BadWindow is expected


class DesktopKeyGrab:
    """Call handler() on each press of the key while a desktop window is focused.

    handler returns True to swallow the key, False to replay it to the desktop.
    on_change(window_ids) is called whenever the set of desktop windows changes,
    including once at startup.
    """

    def __init__(self, handler, key="v", modifier=ControlMask, log=print, on_change=None):
        self.handler = handler
        self.log = log
        self.on_change = on_change   # called with the sorted list of desktop window ids when it changes
        self.grabbed = set()
        self.dpy = None
        if X is None:
            raise RuntimeError("libX11 not found")
        self.dpy = X.XOpenDisplay(os.environ.get("DISPLAY", "").encode() or None)
        if not self.dpy:
            raise RuntimeError("cannot open X display")
        self._err = _ErrorHandler(_ignore_errors)
        X.XSetErrorHandler(self._err)
        self.root = X.XDefaultRootWindow(self.dpy)
        self.keycode = X.XKeysymToKeycode(self.dpy, X.XStringToKeysym(key.encode()))
        self.modifier = modifier
        # grab with every Caps Lock / Num Lock combination so those don't defeat it
        self.mod_combos = [modifier, modifier | LockMask, modifier | Mod2Mask,
                           modifier | LockMask | Mod2Mask]
        self.a_client_list = X.XInternAtom(self.dpy, b"_NET_CLIENT_LIST", False)
        self.a_window_type = X.XInternAtom(self.dpy, b"_NET_WM_WINDOW_TYPE", False)
        self.a_type_desktop = X.XInternAtom(self.dpy, b"_NET_WM_WINDOW_TYPE_DESKTOP", False)
        X.XSelectInput(self.dpy, self.root, PropertyChangeMask)
        self.rescan()
        GLib.io_add_watch(X.XConnectionNumber(self.dpy), GLib.IO_IN, self._on_readable)
        GLib.timeout_add_seconds(10, self._periodic)

    # ---- window discovery ----------------------------------------------
    def _get_property(self, win, atom):
        """Return the property as a list of ints (32-bit format) or None."""
        actual_type = Atom()
        fmt = c.c_int()
        nitems = c.c_ulong()
        after = c.c_ulong()
        data = c.POINTER(c.c_ubyte)()
        ok = X.XGetWindowProperty(self.dpy, win, atom, 0, 4096, False, AnyPropertyType,
                                  c.byref(actual_type), c.byref(fmt), c.byref(nitems),
                                  c.byref(after), c.byref(data))
        if ok != 0 or not data:
            return None
        try:
            if fmt.value != 32:
                return None
            arr = c.cast(data, c.POINTER(c.c_ulong))
            return [arr[i] for i in range(nitems.value)]
        finally:
            X.XFree(data)

    def _candidate_windows(self):
        wins = self._get_property(self.root, self.a_client_list)
        if wins:
            return wins
        # no EWMH window manager (e.g. bare Xvfb): walk the root's children instead
        root_ret, parent_ret = Window(), Window()
        children = c.POINTER(Window)()
        n = c.c_uint()
        if not X.XQueryTree(self.dpy, self.root, c.byref(root_ret), c.byref(parent_ret),
                            c.byref(children), c.byref(n)):
            return []
        try:
            return [children[i] for i in range(n.value)]
        finally:
            if children:
                X.XFree(children)

    def desktop_windows(self):
        found = []
        for w in self._candidate_windows():
            types = self._get_property(w, self.a_window_type) or []
            if self.a_type_desktop in types:
                found.append(w)
        return found

    def rescan(self):
        current = set(self.desktop_windows())
        for w in current - self.grabbed:
            for mods in self.mod_combos:
                X.XGrabKey(self.dpy, self.keycode, mods, w, True, GrabModeAsync, GrabModeSync)
            self.log(f"grabbing key on desktop window 0x{w:x}")
        for w in self.grabbed - current:
            for mods in self.mod_combos:
                X.XUngrabKey(self.dpy, self.keycode, mods, w)
        changed = current != self.grabbed
        self.grabbed = current
        X.XSync(self.dpy, False)
        if changed and self.on_change:
            self.on_change(sorted(current))

    def _periodic(self):
        self.rescan()
        return True

    # ---- events ---------------------------------------------------------
    def _on_readable(self, _fd, _cond):
        self._drain()
        return True

    def _drain(self):
        ev = XEvent()
        while X.XPending(self.dpy):
            X.XNextEvent(self.dpy, c.byref(ev))
            if ev.type == KeyPress:
                k = ev.xkey
                if k.keycode == self.keycode and k.state & self.modifier:
                    swallow = False
                    try:
                        swallow = bool(self.handler())
                    except Exception as e:
                        self.log("key handler failed:", e)
                    X.XAllowEvents(self.dpy, AsyncKeyboard if swallow else ReplayKeyboard, k.time)
                else:
                    X.XAllowEvents(self.dpy, ReplayKeyboard, k.time)
                X.XFlush(self.dpy)
            elif ev.type == PropertyNotify and ev.xproperty.atom == self.a_client_list:
                self.rescan()
