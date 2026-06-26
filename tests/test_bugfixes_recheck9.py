"""Regression tests for the 9th workflow recheck (4 real findings; #3 was a verified
false positive — mss grab dict is logical, image is physical; see docs/BUGLOG.md)."""

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.recovery import MockRecoveryController, Outcome

from helpers import build_player, mock_config, mk_sync


# #w-place: place_tower always emits the placement click (confirm_click=False used to drop it)
def test_place_tower_always_clicks():
    st = S.parse({"events": [{"type": "place_tower", "id": 1, "t_ms": 0,
                             "pos": {"x": 0.41, "y": 0.63}, "confirm_click": False}]}, check_frames=False)
    prims = S.expand_all(st.events)
    clicks = [p for p in prims if p.type == "click"]
    assert len(clicks) == 1 and abs(clicks[0].pos.x - 0.41) < 1e-9  # tower IS placed even with confirm_click=False


# #w-break: humanization break fires only after a COMPLETED run, never on a restart
def test_break_not_taken_on_restart():
    st = S.StratFile(events=[mk_sync(1, 0, "never", timeout=100, on_timeout="recover")], base_dir=".")
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.REJOIN)  # always restarts, never completes
    p, _, _, _ = build_player(
        st, cfg=mock_config(loop_count=0, max_consecutive_restarts=3,
                            break_every_runs=1, break_seconds=9999),
        capture=MockCaptureBackend(current_label="nope"), recovery=rec, clock=FakeClock())
    calls = []
    p._maybe_break_between_runs = lambda: calls.append(1)
    p.run()
    assert p.stats.runs == 0 and calls == []  # no completed run -> break never invoked


def test_break_taken_after_completed_run():
    st = S.StratFile(events=[S.KeyPressEvent(1, 0, "key_press", key="a")], base_dir=".")
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=2, break_every_runs=1, break_seconds=5),
                              clock=FakeClock())
    calls = []
    p._maybe_break_between_runs = lambda: calls.append(1)
    p.run()
    assert p.stats.runs == 2 and len(calls) >= 1  # break invoked after a real completed run
