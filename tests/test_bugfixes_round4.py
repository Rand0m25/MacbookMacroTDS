"""Regression tests for review round 4 (see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.config import Config
from tds_macro.geometry import Coordinates, Rect
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import RecoveryController, FailureMode, Outcome

from helpers import build_player, mock_config, mk_sync
import macfakes as F


# 1) geometry crop box at the far edge stays in-bounds + non-empty
def test_crop_box_edge_in_bounds():
    box = Coordinates.region_crop_box(Rect(0.9998, 0.5, 0.01, 0.05), 1920, 1080)
    x0, y0, x1, y1 = box
    assert 0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080


# 2) config window_rect_override shape validation
@pytest.mark.parametrize("bad", ["1280,720", [100, 200], [1, 2, 3, 4, 5], "1234"])
def test_window_rect_override_bad_raises(bad):
    with pytest.raises(ValueError):
        Config().with_overrides({"window_rect_override": bad})


def test_window_rect_override_good():
    c = Config().with_overrides({"window_rect_override": [0, 0, 800, 600]})
    assert c.window_rect_override == (0, 0, 800, 600)


# 3) NaN/Infinity numerics are reported, not crashing
@pytest.mark.parametrize("val", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_number_reported(val):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "wait", "id": 1, "t_ms": val}]}, check_frames=False)
    assert any("finite" in p for p in ei.value.problems)


# 4) unhashable enum value reported, not crashing
def test_unhashable_enum_reported():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "sync_point", "id": 1, "t_ms": 0, "ref_frame": "f.png",
                             "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "on_timeout": ["abort"]}]},
                check_frames=False)
    assert any("on_timeout" in p and "string" in p for p in ei.value.problems)


# 5) unknown keys in detectors / run_end are reported
@pytest.mark.parametrize("doc,where", [
    ({"events": [], "recovery": {"wrong_map": {"ref_frame": "f.png",
      "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "bogus": 1}}}, "wrong_map"),
    ({"events": [], "run_end": {"timeout_ms": 1000, "bogus": 2}}, "run_end"),
])
def test_detector_unknown_keys_reported(doc, where):
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("unknown field" in p for p in ei.value.problems)


# 6) on_timeout="continue" rebases the clock so later events keep spacing
class _TimedInput(MockInputBackend):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.key_times = {}

    def press_key(self, key, modifiers=()):
        super().press_key(key, modifiers)
        self.key_times[key] = self.clock.now_ms()


def test_continue_timeout_preserves_spacing():
    events = [mk_sync(1, 0, "never", timeout=1000, on_timeout="continue"),
              S.KeyPressEvent(2, 100, "key_press", key="a"),
              S.KeyPressEvent(3, 600, "key_press", key="b")]
    st = S.StratFile(events=events, base_dir=".")
    clk = FakeClock()
    inp = _TimedInput(clk)
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=1),
                                   capture=MockCaptureBackend(current_label="nope"),
                                   clock=clk, input_backend=inp)
    player.run()
    assert inp.key_times["b"] - inp.key_times["a"] >= 499  # not collapsed to 0


# 7) recovery replays ScrollEvent in the leave/reset sequence
def test_recovery_runs_scroll_event():
    seq = [S.ScrollEvent(1, 0, "scroll", pos=S.Point(0.5, 0.5), dx=0, dy=-3)]
    st = S.StratFile(events=[], leave_reset_sequence=seq, base_dir=".")
    inp = MockInputBackend()
    rc = RecoveryController(st, MockWindowProvider(), inp, MockCaptureBackend(),
                            __import__("tds_macro.visual", fromlist=["MockComparator"]).MockComparator(),
                            FakeClock(), mock_config())
    out = rc.handle(FailureMode.WRONG_MAP)
    assert out == Outcome.REJOIN
    assert any(e["action"] == "scroll" for e in inp.events)


# 8) press_key records each key before pressing it (no stuck key on mid-sequence error)
def test_press_key_records_before_press():
    from tds_macro.input_backend import PynputInputBackend
    mods = F.make_pynput()

    class RaisingKb(F.FakeKeyController):
        def press(self, k):
            super().press(k)
            if len(self.pressed) >= 2:
                raise RuntimeError("boom on second press")

    mods["pynput.keyboard"].Controller = RaisingKb
    with F.installed(mods):
        b = PynputInputBackend()
        b._ensure()
        with pytest.raises(RuntimeError):
            b.press_key("a", modifiers=["ctrl"])
        assert {"ctrl", "a"} <= b._held_keys  # both recorded before their press
