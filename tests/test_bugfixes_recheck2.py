"""Regression tests for the 2nd workflow recheck (9 findings; see docs/BUGLOG.md)."""

import json

import pytest

from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.errors import StratValidationError
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import RecoveryController, MockRecoveryController, FailureMode, Outcome
from tds_macro.visual import MockComparator
from tds_macro.frame import Frame

from helpers import build_player, mock_config, mk_sync


# #1 JSON null override on a non-nullable field is rejected (was -> validate() TypeError)
def test_null_override_rejected():
    with pytest.raises(ValueError):
        Config().with_overrides({"sync_poll_ms": None})
    # nullable fields still accept None
    assert Config().with_overrides({"retina_scale_override": None}).retina_scale_override is None


def test_cmd_play_null_override_clean_exit(tmp_path):
    from tds_macro.cli import build_parser
    p = tmp_path / "s.strat.json"
    p.write_text(json.dumps({"events": [], "config_overrides": {"sync_poll_ms": None}}))
    args = build_parser().parse_args(
        ["play", str(p), "--mock", "--dry-run", "--no-frames", "--accept-ban-risk"])
    assert args.func(args) == 1  # clean exit, not an uncaught TypeError


# #2/#3 huge-int coordinate / threshold -> reported, not OverflowError
def test_huge_int_coord_reported():
    huge = int("1" + "0" * 400)
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "click", "id": 1, "t_ms": 0, "pos": {"x": huge, "y": 0.5}}]},
                check_frames=False)
    assert any("too large" in p for p in ei.value.problems)


def test_huge_int_threshold_reported():
    huge = int("1" + "0" * 400)
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "sync_point", "id": 1, "t_ms": 0, "ref_frame": "f.png",
                             "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "threshold": huge}]},
                check_frames=False)
    assert any("too large" in p for p in ei.value.problems)


# #4 huge-int header field coerced to default, no crash
def test_huge_int_header_safe():
    huge = int("1" + "0" * 400)
    st = S.parse({"header": {"window_aspect": huge}, "events": []}, check_frames=False)
    assert st.header.window_aspect == 0.0


# #5 invalid mouse button reported
def test_bad_button_reported():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "click", "id": 1, "t_ms": 0, "pos": {"x": 0.5, "y": 0.5},
                             "button": "lft"}]}, check_frames=False)
    assert any("button" in p and "not one of" in p for p in ei.value.problems)


# #6 recover path absorbs the elapsed timeout into clock_offset (RESUME keeps spacing)
def test_recover_resume_absorbs_timeout():
    st = S.StratFile(events=[], base_dir=".")
    clk = FakeClock()
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.RESUME,
                                 classify_fn=lambda w: FailureMode.NONE)
    p, _, _, _ = build_player(st, cfg=mock_config(), clock=clk,
                              capture=MockCaptureBackend(current_label="x"), recovery=rec)
    p._iter_t0 = 0.0
    p.clock_offset = 0.0
    clk.advance(5000)  # the sync's timeout already elapsed
    p._handle_sync_timeout(mk_sync(1, 1000, "x", on_timeout="recover"))
    assert p.clock_offset >= 4000  # elapsed (5000) - t_ms (1000) absorbed; RESUME won't collapse spacing


# #7 stuck-sync that reclassifies to a CONFIRMED cause self-heals indefinitely (no spurious STOP)
def test_stuck_sync_reclassify_self_heals():
    st = S.StratFile(events=[], recovery=S.RecoverySpec(
        disconnect=S.DetectorSpec("dc.png", S.Rect(0, 0, 1, 1), 0.9),
        lobby_anchor=S.DetectorSpec("lobby.png", S.Rect(0, 0, 1, 1), 0.9)), base_dir=".")
    rc = RecoveryController(st, MockWindowProvider(), __import__(
        "tds_macro.input_backend", fromlist=["MockInputBackend"]).MockInputBackend(),
        MockCaptureBackend(current_label="lobby.png"), MockComparator(), FakeClock(),
        mock_config(max_attempts_per_cause=3))
    scene = Frame.labelled("dc.png")
    outs = [rc.handle(FailureMode.STUCK_SYNC, scene=scene) for _ in range(6)]
    assert all(o == Outcome.REJOIN for o in outs)  # never STOPs when recovery is confirmed
    assert rc.attempts.get("disconnected", 0) == 0
