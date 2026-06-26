"""Regression tests for review round 18 (4 findings, all confirmed genuine):
  #1 capture._clamp_rect_to_bounds: an off-screen window grab is clamped, not crashed
  #2 recorder.run: a pre-run capture_sync_point's epoch is preserved (not rebased)
  #3 recorder pause: Pause/Resume actually excludes input from the recording
  #4 gui.validate: a non-UTF-8 file yields (False, [msg]) instead of raising
"""

from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend, _clamp_rect_to_bounds
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recorder import Recorder
from tds_macro.hotkeys import HotkeyEvents
from tds_macro.strat import load as real_load

from helpers import mock_config
from test_gui_controller import _setup


class _HK:
    """Minimal hotkeys stand-in: just the events the recorder reads."""
    def __init__(self, stop=False):
        self.events = HotkeyEvents()
        if stop:
            self.events.stop.set()  # makes run() exit its loop immediately


def _recorder(hk):
    return Recorder(MockWindowProvider(frontmost=True), MockInputBackend(),
                    MockCaptureBackend(), mock_config(), hotkeys=hk, clock=FakeClock())


# --- #1 grab rect is clamped to the virtual screen (off-screen window can't crash mss) ---
def test_clamp_rect_within_bounds_unchanged():
    b = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    assert _clamp_rect_to_bounds(100, 100, 200, 200, b) == (100, 100, 200, 200)


def test_clamp_rect_partly_offscreen():
    b = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    assert _clamp_rect_to_bounds(-50, -30, 200, 100, b) == (0, 0, 150, 70)


def test_clamp_rect_fully_offscreen_is_min_1px_in_bounds():
    b = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    left, top, w, h = _clamp_rect_to_bounds(5000, 5000, 100, 100, b)
    assert w >= 1 and h >= 1
    assert 0 <= left <= 1920 and 0 <= top <= 1080
    assert left + w <= 1920 and top + h <= 1080


def test_clamp_rect_respects_nonzero_origin_bounds():
    b = {"left": -1920, "top": 0, "width": 1920, "height": 1080}  # a left-hand second monitor
    assert _clamp_rect_to_bounds(-1900, 50, 100, 100, b) == (-1900, 50, 100, 100)


# --- #2 a pre-run sync point's epoch must survive run() (shared epoch, #w12) ---
def test_run_preserves_seeded_epoch(tmp_path):
    rec = _recorder(_HK(stop=True))
    rec._t0 = 1234.0                 # simulate capture_sync_point() having seeded the epoch pre-run
    rec.clock.advance(5000)          # wall time passes before run()
    rec.run(str(tmp_path / "s.json"))
    assert rec._t0 == 1234.0         # run() did NOT rebase the shared epoch


def test_run_seeds_epoch_when_unset(tmp_path):
    rec = _recorder(_HK(stop=True))
    rec.clock.advance(7000)
    assert rec._t0 is None           # unset epoch is None, not 0.0 (round 19 #2)
    rec.run(str(tmp_path / "s.json"))
    assert rec._t0 == 7000           # fresh recorder still gets its epoch from the clock


# --- #3 Pause/Resume excludes input from the recording ---
def test_recorder_pause_excludes_input(tmp_path):
    hk = _HK()
    rec = _recorder(hk)
    rec._refresh_geo()
    rec._on_press("a")               # recorded
    hk.events.pause.set()
    rec._on_press("b")               # dropped (paused)
    rec._on_release("b")             # not tracked -> nothing emitted
    rec._on_scroll(0.5, 0.5, 0, 1)   # dropped (paused)
    hk.events.pause.clear()
    rec._on_press("c")               # recorded again
    st = rec.build(str(tmp_path / "s.json"))
    pressed = [e.key for e in st.events if e.type == "key_press"]
    assert "a" in pressed and "c" in pressed and "b" not in pressed
    assert [e for e in st.events if e.type == "scroll"] == []


def test_recorder_pause_still_delivers_held_release(tmp_path):
    # a key pressed before pause and released DURING pause must still pair (no stuck key)
    hk = _HK()
    rec = _recorder(hk)
    rec._refresh_geo()
    rec._on_press("a")               # recorded; held
    hk.events.pause.set()
    rec._on_release("a")             # release is never gated -> pairs the press
    st = rec.build(str(tmp_path / "s.json"))
    assert [e.type for e in st.events if e.key == "a"] == ["key_press", "key_release"]
    assert not rec._pressed_keys


# --- #4 validate() keeps its (ok, problems) contract on a non-UTF-8 file ---
def test_validate_handles_non_utf8_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_bytes(b"\xff\xfe\x00\x01 not valid utf-8")
    ctrl, _, _ = _setup(load=real_load)
    ok, problems = ctrl.validate(str(p))   # must not raise UnicodeDecodeError
    assert ok is False and problems
