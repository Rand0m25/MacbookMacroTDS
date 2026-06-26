"""Fake macOS / pynput / mss modules so the REAL macOS backend code executes on Linux.

These install into sys.modules so `import Quartz`, `from pynput.mouse import ...`,
`import mss`, `from ApplicationServices import ...` inside the production code run
against controlled stand-ins. This exercises the actual QuartzWindowProvider /
PynputInputBackend / MssCaptureBackend / permissions logic (not mocks of it).
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager


# --------------------------------------------------------------------------- #
# Quartz
# --------------------------------------------------------------------------- #
class _Size:
    def __init__(self, w, h):
        self.width = w
        self.height = h


class _Origin:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Bounds:
    def __init__(self, x, y, w, h):
        self.origin = _Origin(x, y)
        self.size = _Size(w, h)


def make_quartz(windows, display_bounds=(0, 0, 1280, 720), pixels_wide=2560):
    m = types.ModuleType("Quartz")
    m.kCGWindowListOptionOnScreenOnly = 1
    m.kCGWindowListExcludeDesktopElements = 16
    m.kCGNullWindowID = 0
    m.CGWindowListCopyWindowInfo = lambda opts, nullid: list(windows)
    m.CGGetDisplaysWithPoint = lambda point, maxd, a, b: (0, [1], 1)
    m.CGDisplayBounds = lambda did: _Bounds(*display_bounds)
    m.CGDisplayPixelsWide = lambda did: pixels_wide
    m.CGDisplayPixelsHigh = lambda did: int(pixels_wide * display_bounds[3] / display_bounds[2])
    return m


# --------------------------------------------------------------------------- #
# pynput
# --------------------------------------------------------------------------- #
class FakeKey:
    """A special key whose str() looks like pynput's 'Key.<name>'."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"Key.{self.name}"

    def __repr__(self):
        return str(self)


class FakeKeyChar:
    def __init__(self, char):
        self.char = char


_SPECIALS = ("esc enter space tab backspace delete up down left right home end page_up page_down "
             "shift shift_r ctrl ctrl_r alt alt_r cmd cmd_r caps_lock "
             "f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12").split()


class FakeMouseController:
    def __init__(self):
        self.position = (0, 0)
        self.presses = []
        self.releases = []
        self.clicks = []
        self.scrolls = []

    def press(self, b):
        self.presses.append(b)

    def release(self, b):
        self.releases.append(b)

    def click(self, b, count):
        self.clicks.append((b, count))

    def scroll(self, dx, dy):
        self.scrolls.append((dx, dy))

    def move(self, dx, dy):
        self.position = (self.position[0] + dx, self.position[1] + dy)


class FakeKeyController:
    def __init__(self):
        self.pressed = []
        self.released = []
        self.typed = []

    def press(self, k):
        self.pressed.append(k)

    def release(self, k):
        self.released.append(k)

    def type(self, s):
        self.typed.append(s)


class FakeListener:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def make_pynput():
    pynput = types.ModuleType("pynput")
    mouse = types.ModuleType("pynput.mouse")
    keyboard = types.ModuleType("pynput.keyboard")

    mouse.Button = types.SimpleNamespace(left="Button.left", right="Button.right", middle="Button.middle")
    mouse.Controller = FakeMouseController
    mouse.Listener = FakeListener

    key_ns = types.SimpleNamespace(**{name: FakeKey(name) for name in _SPECIALS})
    keyboard.Key = key_ns
    keyboard.KeyCode = types.SimpleNamespace(from_char=lambda c: FakeKeyChar(c),
                                             from_vk=lambda vk: ("vk", vk))
    keyboard.Controller = FakeKeyController
    keyboard.Listener = FakeListener

    class GlobalHotKeys(FakeListener):
        def __init__(self, mapping):
            super().__init__(mapping=mapping)

    keyboard.GlobalHotKeys = GlobalHotKeys

    _KNOWN = set(_SPECIALS)

    class HotKey:
        @staticmethod
        def parse(combo):
            import re
            if not combo:
                raise ValueError("empty hotkey")
            for tok in combo.split("+"):
                m = re.fullmatch(r"<([^>]+)>", tok)
                if m:
                    if m.group(1) not in _KNOWN:
                        raise ValueError(f"unknown key {m.group(1)!r}")
                elif len(tok) != 1:
                    raise ValueError(f"bad token {tok!r}")
            return []

    keyboard.HotKey = HotKey

    pynput.mouse = mouse
    pynput.keyboard = keyboard
    return {"pynput": pynput, "pynput.mouse": mouse, "pynput.keyboard": keyboard}


# --------------------------------------------------------------------------- #
# mss
# --------------------------------------------------------------------------- #
def make_mss(scale=1):
    import numpy as np

    m = types.ModuleType("mss")

    class FakeSct:
        def __init__(self):
            self.grabs = []
            # monitors[0] is the bounding box of all monitors (points); large enough that
            # normal in-bounds grabs pass through _clamp_rect_to_bounds unchanged.
            self.monitors = [{"left": 0, "top": 0, "width": 5120, "height": 2880}]

        def grab(self, region):
            self.grabs.append(dict(region))
            h = int(region["height"] * scale)
            w = int(region["width"] * scale)
            return np.zeros((h, w, 4), dtype=np.uint8)

        def close(self):
            pass

    m.mss = lambda: FakeSct()
    return m


# --------------------------------------------------------------------------- #
# ApplicationServices (permissions)
# --------------------------------------------------------------------------- #
def make_appservices(trusted=True):
    m = types.ModuleType("ApplicationServices")
    m.kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"
    m.AXIsProcessTrusted = lambda: trusted
    m.AXIsProcessTrustedWithOptions = lambda opts: trusted
    return m


@contextmanager
def installed(modules: dict):
    """Temporarily install fake modules into sys.modules."""
    saved = {}
    for name, mod in modules.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
