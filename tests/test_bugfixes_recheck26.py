"""Regression tests for the 3 deferred mediums the user asked to fix before stopping:
  #6 _wait_run_end final poll catches a victory that appeared in the last poll gap
  #9 recovery move/drag pass the clock so they're panic-interruptible
  #11 _phash_sim doesn't falsely match two flat frames of different brightness
"""

import pytest

import macfakes as F
from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend as _MockInput
from tds_macro.window import MockWindowProvider
from tds_macro.visual import MockComparator, NumpyComparator
from tds_macro.recovery import RecoveryController, FailureMode
from tds_macro.config import MatchMethod
from tds_macro.errors import PanicAbort
from tds_macro.geometry import Point

from helpers import build_player, mock_config


# --- #6 a victory that appears in the gap after the last poll is still detected ---
def test_run_end_final_poll_catches_victory():
    clk = FakeClock()
    cap = MockCaptureBackend(current_label="nomatch")
    st = S.StratFile(base_dir=".", run_end=S.RunEnd(
        victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=100))

    def on_sleep(now):
        if now >= 100:                 # past the deadline -> the end screen is now showing
            cap.current_label = "victory"

    clk.on_sleep = on_sleep
    p, _, _, _ = build_player(st, cfg=mock_config(recovery_check_every_ms=30),
                              capture=cap, clock=clk, window=MockWindowProvider(frontmost=True))
    assert p._wait_run_end() == FailureMode.VICTORY   # caught by the post-loop check, not NONE


# --- #9 recovery move/drag are panic-interruptible (clock threaded through) ---
def test_recovery_run_sequence_passes_clock_to_move():
    captured = {}

    class _Spy(_MockInput):
        def move(self, px, py, duration_ms=0, hz=120, clock=None, should_abort=None, easing="linear"):
            captured["clock"] = clock
            return super().move(px, py, duration_ms, hz, clock, should_abort, easing)

    clk = FakeClock()
    rc = RecoveryController(S.StratFile(base_dir="."), MockWindowProvider(), _Spy(),
                            MockCaptureBackend(), MockComparator(), clk, mock_config())
    rc._run_sequence([S.MouseMoveEvent(1, 0, "mouse_move", "", None, Point(0.5, 0.5), 100, "linear")])
    assert captured["clock"] is clk


def test_pynput_move_aborts_via_clock():
    with F.installed(F.make_pynput()):
        from tds_macro.input_backend import PynputInputBackend
        from tds_macro.clock import RealClock
        b = PynputInputBackend()
        b._ensure()
        clk = RealClock(should_abort=lambda: True)   # panic active
        with pytest.raises(PanicAbort):
            b.move(0.0, 0.0, 100, clock=clk)         # interpolation sleep checks abort -> raises


# --- #11 phash must not match two uniform frames of different brightness ---
def test_phash_flat_frames_dont_falsely_match():
    import numpy as np

    from tds_macro.frame import Frame
    cmp = NumpyComparator()
    black = Frame.from_numpy(np.zeros((20, 20, 3), dtype="uint8"))
    white = Frame.from_numpy(np.full((20, 20, 3), 255, dtype="uint8"))
    assert cmp.score(black, white, MatchMethod.PHASH) < 0.5      # different flat levels -> no match
    assert cmp.score(black, black, MatchMethod.PHASH) >= 0.99    # identical flat -> match
