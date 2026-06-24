"""Run the REAL macOS-targeted backend code on Linux by injecting fake OS modules.

This is the closest we can get to 'testing on a MacBook' without macOS: the actual
QuartzWindowProvider / PynputInputBackend / MssCaptureBackend / permissions code
executes, with Quartz/pynput/mss/ApplicationServices stubbed to controlled values.
"""

import pytest

from tds_macro.config import Config, WindowBackendKind
from tds_macro.clock import FakeClock
from tds_macro.errors import PanicAbort

import macfakes as F


# --------------------------------------------------------------------------- #
# QuartzWindowProvider
# --------------------------------------------------------------------------- #
def test_quartz_picks_largest_layer0_roblox_window():
    windows = [
        {"kCGWindowOwnerName": "Dock", "kCGWindowLayer": 20,
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 3000, "Height": 40}},
        {"kCGWindowOwnerName": "Roblox", "kCGWindowLayer": 0,
         "kCGWindowBounds": {"X": 100, "Y": 50, "Width": 1600, "Height": 900}},
        {"kCGWindowOwnerName": "Roblox", "kCGWindowLayer": 0,
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 300, "Height": 200}},  # smaller helper
        {"kCGWindowOwnerName": "Safari", "kCGWindowLayer": 0,
         "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 2000, "Height": 1400}},
    ]
    quartz = F.make_quartz(windows, display_bounds=(0, 0, 1280, 720), pixels_wide=2560)
    with F.installed({"Quartz": quartz}):
        from tds_macro.window import QuartzWindowProvider
        prov = QuartzWindowProvider(Config(window_backend=WindowBackendKind.QUARTZ))
        geo = prov.get_geometry()
    assert (geo.x, geo.y, geo.w, geo.h) == (100, 50, 1600, 900)  # the big Roblox window
    assert geo.retina == 2.0  # 2560 px / 1280 logical


def test_quartz_retina_override_respected():
    windows = [{"kCGWindowOwnerName": "Roblox", "kCGWindowLayer": 0,
                "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 800, "Height": 600}}]
    quartz = F.make_quartz(windows, display_bounds=(0, 0, 800, 600), pixels_wide=800)
    with F.installed({"Quartz": quartz}):
        from tds_macro.window import QuartzWindowProvider
        cfg = Config(window_backend=WindowBackendKind.QUARTZ, retina_scale_override=1.5)
        geo = QuartzWindowProvider(cfg).get_geometry()
    assert geo.retina == 1.5


def test_quartz_raises_when_no_roblox():
    from tds_macro.errors import WindowNotFoundError
    quartz = F.make_quartz([{"kCGWindowOwnerName": "Finder", "kCGWindowLayer": 0,
                             "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 100, "Height": 100}}])
    with F.installed({"Quartz": quartz}):
        from tds_macro.window import QuartzWindowProvider
        with pytest.raises(WindowNotFoundError):
            QuartzWindowProvider(Config(window_backend=WindowBackendKind.QUARTZ)).get_geometry()


# --------------------------------------------------------------------------- #
# PynputInputBackend
# --------------------------------------------------------------------------- #
def _pynput_backend():
    from tds_macro.input_backend import PynputInputBackend
    b = PynputInputBackend()
    b._ensure()
    return b


def test_pynput_move_teleport_and_interpolate():
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        b.move(123, 456)  # teleport
        assert b._mouse.position == (123, 456)
        clk = FakeClock()
        b.move(0, 0, duration_ms=100, hz=10, clock=clk)  # interpolate
        assert b._mouse.position == (0, 0)
        assert clk.now_ms() >= 100  # slept through the interpolation


def test_pynput_move_aborts_on_panic():
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        with pytest.raises(PanicAbort):
            b.move(500, 500, duration_ms=100, hz=10, clock=FakeClock(), should_abort=lambda: True)


def test_pynput_click_tracks_and_releases():
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        b.click("left", 10, 20, clicks=1, hold_ms=0)
        assert b._mouse.position == (10, 20)
        assert b._mouse.presses and b._mouse.releases
        assert not b._held_buttons  # released, nothing stuck


def test_pynput_double_click_uses_count():
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        b.click("left", 5, 5, clicks=2)
        assert b._mouse.clicks == [("Button.left", 2)]  # true double-click, not 2 singles


def test_pynput_drag_releases_even_on_panic():
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        with pytest.raises(PanicAbort):
            b.drag("left", 0, 0, 100, 100, duration_ms=50, hz=10, clock=FakeClock(),
                   should_abort=lambda: True)
        assert not b._held_buttons  # finally-released despite the panic (R22)
        assert b._mouse.releases  # release was actually issued


def test_pynput_key_codec_and_release_all():
    from tds_macro.input_backend import key_to_pynput, pynput_to_name
    with F.installed(F.make_pynput()):
        from pynput.keyboard import Key
        assert key_to_pynput("esc") is Key.esc
        assert key_to_pynput("a") == "a"
        assert pynput_to_name(Key.space) == "space"
        assert pynput_to_name(F.FakeKeyChar("q")) == "q"

        b = _pynput_backend()
        b.press_key("e")
        b.press_key("shift")
        assert b._held_keys == {"e", "shift"}
        b.release_all()
        assert not b._held_keys
        b.release_all()  # idempotent


def test_pynput_listeners_translate_events():
    seen = {}
    with F.installed(F.make_pynput()):
        b = _pynput_backend()
        b.start_listeners(
            on_move=lambda x, y: seen.setdefault("move", (x, y)),
            on_click=lambda x, y, btn, pressed: seen.setdefault("click", (x, y, btn, pressed)),
            on_press=lambda k: seen.setdefault("press", k),
        )
        # drive the fake listeners' callbacks as pynput would
        ml = b._mouse_listener
        ml.kwargs["on_move"](7, 8)
        ml.kwargs["on_click"](1, 2, "Button.right", True)
        b._kb_listener.kwargs["on_press"](F.FakeKey("enter"))
    assert seen["move"] == (7, 8)
    assert seen["click"] == (1, 2, "right", True)  # Button-name normalized
    assert seen["press"] == "enter"               # Key name normalized


# --------------------------------------------------------------------------- #
# MssCaptureBackend
# --------------------------------------------------------------------------- #
def test_mss_region_math_and_shape():
    from tds_macro.capture import MssCaptureBackend
    from tds_macro.geometry import WindowGeometry, Rect
    with F.installed({"mss": F.make_mss(scale=2)}):  # Retina 2x
        cap = MssCaptureBackend()
        geo = WindowGeometry(100, 50, 1600, 900, retina=2.0)
        frame = cap.grab_region(geo, Rect(0.5, 0.0, 0.25, 0.1))
        sct = cap._local.sct
        assert sct.grabs[-1] == {"left": 900, "top": 50, "width": 400, "height": 90}
        # returned image is physical (2x) pixels
        assert frame.width == 800 and frame.height == 180


# --------------------------------------------------------------------------- #
# permissions (the macOS branch)
# --------------------------------------------------------------------------- #
class _FlatCapture:
    def grab_window(self, geo):
        from tds_macro.frame import Frame
        return Frame.labelled("black")  # zeros -> variance 0


class _BusyCapture:
    def grab_window(self, geo):
        import numpy as np
        from tds_macro.frame import Frame
        return Frame.from_numpy(np.random.default_rng(1).integers(0, 255, (8, 8, 4), dtype="uint8"))


def test_permissions_macos_branch(monkeypatch):
    from tds_macro import permissions
    from tds_macro.geometry import WindowGeometry
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    geo = WindowGeometry(0, 0, 100, 100)
    with F.installed({"ApplicationServices": F.make_appservices(trusted=True)}):
        assert permissions.check_accessibility(prompt=True) is True
    with F.installed({"ApplicationServices": F.make_appservices(trusted=False)}):
        assert permissions.check_accessibility() is False
    # Screen Recording heuristic: black frame -> missing, busy frame -> ok
    assert permissions.check_screen_recording(_FlatCapture(), geo) is False
    assert permissions.check_screen_recording(_BusyCapture(), geo) is True
