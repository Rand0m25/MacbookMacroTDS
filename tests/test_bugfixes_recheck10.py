"""Regression tests for the 11th workflow recheck (3 findings; see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import MockRecoveryController, FailureMode, Outcome
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

from helpers import build_player, mock_config


# #1 a fractional float in an int field is reported, not silently truncated
@pytest.mark.parametrize("ev,field", [
    ({"type": "wait", "id": 1, "t_ms": 100.9}, "t_ms"),
    ({"type": "wait", "id": 2.9, "t_ms": 0}, "id"),
    ({"type": "click", "id": 1, "t_ms": 0, "pos": {"x": 0.5, "y": 0.5}, "clicks": 1.5}, "clicks"),
])
def test_fractional_int_field_reported(ev, field):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [ev]}, check_frames=False)
    assert any(field in p and "whole number" in p for p in ei.value.problems)


def test_integral_float_still_accepted():
    st = S.parse({"events": [{"type": "wait", "id": 1, "t_ms": 100.0}]}, check_frames=False)
    assert st.events[0].t_ms == 100  # 100.0 is integral -> fine


# #2 a slow refocus during run-end extends the deadline so a real outcome isn't missed
def test_run_end_deadline_extended_after_refocus():
    clk = FakeClock()
    win = MockWindowProvider(frontmost=False)  # focus lost at run-end start
    cap = MockCaptureBackend(current_label="nope")

    def slow_refocus(reason, scene):
        clk.advance(150)                 # refocus consumes 150ms (> the 100ms timeout)
        win.frontmost = True
        cap.current_label = "victory"    # the match actually ended (victory) during refocus
        return Outcome.RESUME

    rec = MockRecoveryController(handle_fn=slow_refocus)
    st = S.StratFile(events=[], run_end=S.RunEnd(
        victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=100), base_dir=".")
    p, _, _, _ = build_player(st, cfg=mock_config(recovery_check_every_ms=10),
                              capture=cap, clock=clk, recovery=rec, window=win)
    fm = p._wait_run_end()
    assert fm == FailureMode.VICTORY  # was NONE (outcome missed) before the deadline extension


# #3 capture_sync_point is safe before run() (guards _geo is None)
def test_capture_sync_point_before_run(tmp_path):
    from tds_macro.recorder import Recorder
    rec = Recorder(MockWindowProvider(rect=(0, 0, 100, 100)), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()))
    rec._strat_dir = str(tmp_path)
    sp = rec.capture_sync_point()  # never called run() -> _geo is None -> must self-refresh, not crash
    assert sp.type == "sync_point"
