"""Regression tests for the 6th workflow recheck (4 findings; see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import RecoveryController, FailureMode, Outcome
from tds_macro.visual import MockComparator
from tds_macro.engine import WaitResult
from tds_macro.frame import Frame

from helpers import build_player, mock_config, mk_sync


# #w1 ability confirm:true without confirm_pos is rejected
def test_ability_confirm_requires_pos():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "ability", "id": 1, "t_ms": 0,
                             "tower_pos": {"x": 0.1, "y": 0.1}, "ability_button_pos": {"x": 0.2, "y": 0.2},
                             "confirm": True}]}, check_frames=False)
    assert any("confirm_pos" in p for p in ei.value.problems)


def test_ability_confirm_with_pos_ok():
    st = S.parse({"events": [{"type": "ability", "id": 1, "t_ms": 0,
                             "tower_pos": {"x": 0.1, "y": 0.1}, "ability_button_pos": {"x": 0.2, "y": 0.2},
                             "confirm": True, "confirm_pos": {"x": 0.3, "y": 0.3}}]}, check_frames=False)
    assert st.events[0].confirm is True


# #w2 require_settled must not hang when the ROI already matches at barrier entry
def test_require_settled_already_matching_fires():
    st = S.StratFile(events=[], base_dir=".")
    cap = MockCaptureBackend(current_label="s")  # already showing the target at entry
    p, _, _, _ = build_player(st, cfg=mock_config(), capture=cap, clock=FakeClock())
    sync = mk_sync(1, 0, "s", require_settled=True, stability_frames=1, timeout=5000)
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # was TIMEOUT before (seen_low never set)


# #w3 DEFEAT/VICTORY never charge a recovery budget -> healthy bot never STOPs
@pytest.mark.parametrize("mode", [FailureMode.DEFEAT, FailureMode.VICTORY])
def test_run_end_modes_dont_exhaust_budget(mode):
    rc = RecoveryController(S.StratFile(events=[], base_dir="."), MockWindowProvider(),
                            MockInputBackend(), MockCaptureBackend(), MockComparator(), FakeClock(),
                            mock_config(max_attempts_per_cause=2))
    for _ in range(6):
        assert rc.handle(mode) == Outcome.REJOIN
    assert rc.attempts.get(mode.value, 0) == 0  # never charged


# #w4 a stuck-sync that reclassifies to DEFEAT/VICTORY doesn't spuriously STOP
def test_stuck_sync_reclassify_to_defeat_no_stop():
    st = S.StratFile(events=[], run_end=S.RunEnd(
        defeat=S.DetectorSpec("defeat.png", S.Rect(0, 0, 1, 1), 0.9)), base_dir=".")
    rc = RecoveryController(st, MockWindowProvider(), MockInputBackend(), MockCaptureBackend(),
                            MockComparator(), FakeClock(), mock_config(max_attempts_per_cause=2))
    scene = Frame.labelled("defeat.png")  # classify(scene) -> DEFEAT
    for _ in range(6):
        assert rc.handle(FailureMode.STUCK_SYNC, scene=scene) == Outcome.REJOIN
    assert rc.attempts.get("defeat", 0) == 0
