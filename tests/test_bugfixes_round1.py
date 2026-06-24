"""Regression tests for the 15 defects found in review round 1 (see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.clock import FakeClock
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import MockRecoveryController, Outcome
from tds_macro.geometry import Point

from helpers import build_player, mock_config, mk_sync


# --- strat numeric/type hardening (D1-D6) ---
@pytest.mark.parametrize("event", [
    {"id": 1, "t_ms": 0, "type": "wait", "jitter_ms": "abc"},
    {"id": 1, "t_ms": 0, "type": "click", "pos": {"x": 0.5, "y": 0.5}, "clicks": "two"},
    {"id": 1, "t_ms": 0, "type": "place_tower", "pos": {"x": 0.1, "y": 0.1}, "hotbar_slot": "x"},
    {"id": 1, "t_ms": 0, "type": "place_tower", "pos": {"x": 0.1, "y": 0.1},
     "expect": {"ref_frame": "f.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "threshold": "high"}},
    {"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "f.png",
     "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "timeout_ms": "soon"},
    {"id": 1, "t_ms": "later", "type": "wait"},
])
def test_bad_numeric_is_collected_not_crash(event):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [event]}, check_frames=False)
    assert any("must be a number" in p for p in ei.value.problems)


def test_non_dict_header_does_not_crash():
    st = S.parse({"header": "oops", "events": []}, check_frames=False)  # must not AttributeError
    assert st.header.map == ""


def test_bad_detector_threshold_collected():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [], "recovery": {"wrong_map": {
            "ref_frame": "f.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "threshold": "hi"}}},
            check_frames=False)
    assert any("must be a number" in p for p in ei.value.problems)


# --- engine D7: pause absorbs wall time (spacing preserved) ---
def test_pause_absorbs_wall_time():
    hk = HotkeyManager(mock_config(), HotkeyEvents())
    hk.events.pause.set()
    clk = FakeClock(on_sleep=lambda now: hk.events.pause.clear() if now >= 300 else None)
    st = S.StratFile(events=[], base_dir=".")
    player, _, _, _ = build_player(st, cfg=mock_config(), clock=clk, hotkeys=hk)
    player._iter_t0 = 0.0
    player.clock_offset = 0.0
    player._maybe_pause()
    assert player.clock_offset >= 300  # pause time folded into the monotonic offset


# --- engine D8: retry actually retries then escalates ---
def test_retry_budget_then_escalates():
    st = S.StratFile(events=[mk_sync(1, 0, "never", timeout=200, on_timeout="retry")], base_dir=".")
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.STOP)
    player, _, _, _ = build_player(
        st, cfg=mock_config(loop_count=1, sync_max_retries=2),
        capture=MockCaptureBackend(current_label="nope"), recovery=rec)
    stats = player.run()
    # initial attempt + 2 retries = 3 timeouts, then recovery escalation
    assert stats.sync_timeouts == 3
    assert rec.handle_calls


# --- visual D10/D11: flat frame must not match a textured one ---
def test_flat_frame_does_not_match_textured():
    np = pytest.importorskip("numpy")
    from tds_macro.visual import NumpyComparator
    from tds_macro.frame import Frame
    from tds_macro.config import MatchMethod
    cmp = NumpyComparator()
    flat = Frame.from_numpy(np.full((16, 16, 4), 128, dtype=np.uint8))
    black = Frame.from_numpy(np.zeros((16, 16, 4), dtype=np.uint8))
    textured = Frame.from_numpy(np.random.default_rng(3).integers(0, 255, (16, 16, 4), dtype=np.uint8))
    for method in (MatchMethod.TM_CCOEFF_NORMED, MatchMethod.NCC):
        assert cmp.score(flat, textured, method) < 0.5     # one flat -> no match
    assert cmp.score(black, textured, MatchMethod.TM_SQDIFF_NORMED) < 0.5
    # genuine matches still hold
    assert cmp.score(black, black, MatchMethod.TM_SQDIFF_NORMED) > 0.99
    assert cmp.score(flat, Frame.from_numpy(np.full((16, 16, 4), 128, dtype=np.uint8)),
                     MatchMethod.NCC) > 0.99


# --- recorder D14/D15: key pairing + out-of-window release delivered ---
def _recorder(frontmost=True):
    from tds_macro.recorder import Recorder
    win = MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=frontmost)
    rec = Recorder(win, MockInputBackend(), MockCaptureBackend(), mock_config(),
                   HotkeyManager(mock_config(), HotkeyEvents()), clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    return rec, win


def test_key_press_gated_and_release_always_paired():
    rec, win = _recorder(frontmost=True)
    rec._on_press("a")
    win.frontmost = False           # focus lost mid-hold
    rec._on_release("a")            # release still recorded (press was recorded)
    assert [e.type for e in rec.coalescer.finish()] == ["key_press", "key_release"]


def test_key_press_while_not_frontmost_ignored():
    rec, win = _recorder(frontmost=False)
    rec._on_press("b")
    rec._on_release("b")
    assert rec.coalescer.finish() == []


def test_out_of_window_release_not_dropped():
    rec, win = _recorder(frontmost=True)
    rec._on_click(500, 500, "left", True)        # press inside
    rec._on_click(99999, 99999, "left", False)   # release far outside the window
    evs = rec.coalescer.finish()
    assert len(evs) == 1 and evs[0].type in ("drag", "click")  # not stuck, no leftover press
    assert "left" not in rec.coalescer._down