"""Regression tests for the 5th workflow recheck (4 findings; see docs/BUGLOG.md)."""


from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import RecoveryController, FailureMode
from tds_macro.visual import MockComparator
from tds_macro.engine import Player
from tds_macro.frame import Frame
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.geometry import WindowGeometry

from helpers import mock_config
import macfakes as F


# #w1 _wait_run_end re-acquires focus instead of silently ignoring FOCUS_LOST
def test_wait_run_end_refocuses_on_focus_loss():
    st = S.StratFile(events=[], run_end=S.RunEnd(
        defeat=S.DetectorSpec("defeat.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=40), base_dir=".")
    win = MockWindowProvider(frontmost=False)   # focus lost during the wait
    cap = MockCaptureBackend(current_label="nope")
    cfg = mock_config(recovery_check_every_ms=10)
    clk = FakeClock()
    rec = RecoveryController(st, win, MockInputBackend(), cap, MockComparator(), clk, cfg)
    p = Player(st, win, MockInputBackend(), cap, MockComparator(), clk, rec, cfg,
               ref_loader=lambda path: Frame.labelled(""))
    fm = p._wait_run_end()
    assert win.activate_calls >= 1            # refocus actually attempted
    assert fm == FailureMode.NONE             # returned normally, didn't hang/misclassify


# #w2 a near-uniform but non-black frame counts as Screen-Recording-granted
def test_screen_recording_not_false_negative(monkeypatch):
    import numpy as np
    from tds_macro import permissions
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    geo = WindowGeometry(0, 0, 8, 8)

    class _Cap:
        def __init__(self, arr):
            self.arr = arr

        def grab_window(self, g):
            return Frame.from_numpy(self.arr)

    grey = _Cap(np.full((8, 8, 4), 60, dtype=np.uint8))     # solid grey: var 0, mean 60
    black = _Cap(np.zeros((8, 8, 4), dtype=np.uint8))       # flat black: var 0, mean 0
    assert permissions.check_screen_recording(grey, geo) is True   # was wrongly False before
    assert permissions.check_screen_recording(black, geo) is False  # genuine denial still caught


# #w3 a hotkey combo collision never overwrites the panic key
def test_hotkey_collision_keeps_panic():
    with F.installed(F.make_pynput()):
        cfg = mock_config(panic_hotkey="f8", pause_hotkey="f8",  # collision!
                          start_hotkey="f9", mark_sync_hotkey="f10")
        hk = HotkeyManager(cfg, HotkeyEvents())
        try:
            hk.start()
            mapping = hk._listener.kwargs["mapping"]
            assert mapping["<f8>"] == hk._on_panic  # panic kept, not clobbered by pause
        finally:
            hk.stop()


# #w4 a drag that leaves the window mid-press is still recorded as a drag, not a click
def test_drag_out_of_window_not_misclassified():
    from tds_macro.recorder import Recorder
    win = MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True)
    rec = Recorder(win, MockInputBackend(), MockCaptureBackend(), mock_config(),
                   HotkeyManager(mock_config(), HotkeyEvents()), clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    rec._on_click(500, 500, "left", True)   # press at center
    rec._on_move(5000, 500)                 # drag far out of the window (x huge)
    rec._on_click(500, 500, "left", False)  # release back near press point
    evs = rec.coalescer.finish()
    assert len(evs) == 1 and evs[0].type == "drag"  # excursion tracked -> drag, not click
