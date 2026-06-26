"""Regression tests for the 9 findings from the workflow recheck (docs/BUGLOG.md)."""

import threading
import time

import pytest

from tds_macro import strat as S
from tds_macro.config import Config, WindowBackendKind
from tds_macro.errors import StratValidationError, WindowNotFoundError
from tds_macro.geometry import Coordinates, WindowGeometry
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider, QuartzWindowProvider
from tds_macro.recovery import RecoveryController
from tds_macro.visual import MockComparator
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

from helpers import build_player, mock_config
import macfakes as F


# #1 str config field given a non-string is coerced (no AttributeError on str consumers)
def test_str_config_field_coerced():
    c = Config().with_overrides({"window_title_match": 123})
    assert c.window_title_match == "123" and isinstance(c.window_title_match, str)


# #2 boolean fields reject quoted strings instead of silently inverting
@pytest.mark.parametrize("ev,field", [
    ({"type": "place_tower", "id": 1, "t_ms": 0, "pos": {"x": 0.4, "y": 0.6}, "confirm_click": "false"}, "confirm_click"),
    ({"type": "ability", "id": 1, "t_ms": 0, "tower_pos": {"x": 0.1, "y": 0.1},
      "ability_button_pos": {"x": 0.2, "y": 0.2}, "confirm": "no"}, "confirm"),
    ({"type": "sync_point", "id": 1, "t_ms": 0, "ref_frame": "f.png",
      "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "require_settled": "x"}, "require_settled"),
])
def test_bool_string_rejected(ev, field):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [ev]}, check_frames=False)
    assert any(field in p and "boolean" in p for p in ei.value.problems)


def test_real_bools_still_parse():
    st = S.parse({"events": [{"type": "place_tower", "id": 1, "t_ms": 0,
                             "pos": {"x": 0.4, "y": 0.6}, "confirm_click": False}]}, check_frames=False)
    assert st.events[0].confirm_click is False


# #3 negative settle_ms / between_ms rejected
@pytest.mark.parametrize("ev,needle", [
    ({"type": "place_tower", "id": 1, "t_ms": 0, "pos": {"x": 0.4, "y": 0.6}, "settle_ms": -5}, "settle_ms"),
    ({"type": "upgrade", "id": 1, "t_ms": 0, "target_pos": {"x": 0.5, "y": 0.5},
      "upgrade_button_pos": {"x": 0.9, "y": 0.7}, "between_ms": -5}, "between_ms"),
])
def test_negative_delay_rejected(ev, needle):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [ev]}, check_frames=False)
    assert any(needle in p and ">= 0" in p for p in ei.value.problems)


# #4 break timer must not fire when no run has completed
def test_no_break_at_zero_runs():
    clk = FakeClock()
    p, _, _, _ = build_player(S.StratFile(events=[], base_dir="."),
                              cfg=mock_config(break_every_runs=1, break_seconds=5), clock=clk)
    p.stats.runs = 0
    p._maybe_break_between_runs()
    assert clk.now_ms() == 0  # no break at runs=0
    p.stats.runs = 2
    p._maybe_break_between_runs()
    assert clk.now_ms() == 5000  # break taken after a real completed run


# #5 recovery leave/reset honors recorded inter-event timing
def test_recovery_sequence_honors_timing():
    seq = [S.ClickEvent(1, 0, "click", pos=S.Point(0.1, 0.1)),
           S.ClickEvent(2, 800, "click", pos=S.Point(0.2, 0.2))]
    clk = FakeClock()
    rc = RecoveryController(S.StratFile(events=[], leave_reset_sequence=seq, base_dir="."),
                            MockWindowProvider(), MockInputBackend(), MockCaptureBackend(),
                            MockComparator(), clk, mock_config())
    t0 = clk.now_ms()
    rc._run_sequence(seq)
    assert clk.now_ms() - t0 >= 800  # the 800ms gap is reproduced, not fired back-to-back


# #6 a zero-size (minimized) Roblox window is not selected -> reported as not found
def test_zero_area_window_not_selected():
    q = F.make_quartz([{"kCGWindowOwnerName": "Roblox", "kCGWindowLayer": 0,
                        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 0, "Height": 0}}])
    with F.installed({"Quartz": q}):
        with pytest.raises(WindowNotFoundError):
            QuartzWindowProvider(Config(window_backend=WindowBackendKind.QUARTZ)).get_geometry()


# #7 a transient window-lookup failure mid-recording doesn't abort the recording
def test_recording_survives_transient_window_loss(tmp_path):
    from tds_macro.recorder import Recorder

    class _FlakyWin:
        def __init__(self):
            self.n = 0

        def get_geometry(self):
            self.n += 1
            if self.n <= 1:
                return WindowGeometry(0, 0, 1000, 1000)
            raise RuntimeError("window vanished mid-record")

        def is_frontmost(self):
            return True

        def activate(self):
            pass

    hk = HotkeyManager(mock_config(), HotkeyEvents())
    rec = Recorder(_FlakyWin(), MockInputBackend(), MockCaptureBackend(), mock_config(), hk)
    box = {}
    t = threading.Thread(target=lambda: box.update(st=rec.run(str(tmp_path / "r.strat.json"))))
    t.start()
    time.sleep(0.2)
    hk.events.stop.set()
    t.join(3)
    assert "st" in box  # run() returned a StratFile despite in-loop get_geometry raising


# #8 degenerate zero-size window doesn't crash coordinate conversion
def test_logical_to_norm_zero_size_no_crash():
    p = Coordinates(WindowGeometry(0, 0, 0, 0)).logical_to_norm(5, 5)  # no ZeroDivisionError
    assert isinstance(p.x, float)
    Coordinates(WindowGeometry(0, 0, 0, 0, retina=0)).physical_to_norm(5, 5)  # no div-by-zero


# #9 calibrate reports a missing sync frame instead of crashing
def test_calibrate_missing_frame_reported(tmp_path):
    import json
    from tds_macro.cli import build_parser
    doc = {"events": [{"type": "sync_point", "id": 1, "t_ms": 0, "label": "w",
                       "ref_frame": "frames/missing.png", "region": {"x": 0, "y": 0, "w": 0.1, "h": 0.1}}]}
    p = tmp_path / "s.strat.json"
    p.write_text(json.dumps(doc))
    args = build_parser().parse_args(["calibrate", str(p), "--mock", "--no-frames"])
    assert args.func(args) == 0  # missing frame reported, no uncaught traceback
