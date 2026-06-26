"""Regression tests for review round 16 (6 raised; #6 consent-persist rejected as
working-as-intended — matches the CLI's --accept-ban-risk; see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.pngio import write_png, read_png
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.visual import MockComparator
from tds_macro.frame import Frame
from tds_macro.engine import RunState
from tds_macro.recovery import RecoveryController, MockRecoveryController, FailureMode, Outcome

from helpers import build_player, mock_config


# #1 a truncated PNG chunk header/body raises a clear ValueError (not struct.error)
def test_read_png_truncated_chunk(tmp_path):
    p = str(tmp_path / "ok.png")
    write_png(p, bytes([1, 2, 3, 255]) * (2 * 2), 2, 2, 4)
    raw = open(p, "rb").read()
    for cut, word in [(10, "header"), (20, "body")]:
        bad = str(tmp_path / f"bad{cut}.png")
        open(bad, "wb").write(raw[:cut])
        with pytest.raises(ValueError) as ei:
            read_png(bad)
        assert word in str(ei.value)


# #2 _route_recovery restores the phase it was called from on RESUME (not hard IN_MATCH)
def test_route_recovery_restores_state_on_resume():
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.RESUME)
    p, _, _, _ = build_player(S.StratFile(base_dir="."), recovery=rec, clock=FakeClock())
    p.state = RunState.LOBBY
    p._route_recovery(FailureMode.FOCUS_LOST)
    assert p.state == RunState.LOBBY  # restored, not flipped to IN_MATCH


# #3 persistent focus-flapping can't grow the run-end deadline without bound
def test_wait_run_end_bounds_focus_extension():
    clk = FakeClock()
    win = MockWindowProvider(frontmost=False)  # focus never stabilizes
    calls = [0]

    def refocus(reason, scene):
        calls[0] += 1
        clk.advance(1000)  # each refocus burns 1000ms but window stays not-frontmost
        return Outcome.RESUME

    rec = MockRecoveryController(handle_fn=refocus)
    st = S.StratFile(base_dir=".", run_end=S.RunEnd(
        victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=100))
    p, _, _, _ = build_player(st, cfg=mock_config(recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="nomatch"),
                              clock=clk, recovery=rec, window=win)
    fm = p._wait_run_end()
    assert fm == FailureMode.NONE   # terminates (would hang unbounded without the cap)
    assert calls[0] <= 3            # extension capped -> only a couple of refocus attempts


# #4 reclassifying a captured frame must not derive FOCUS_LOST from live focus
def test_classify_live_flag():
    win = MockWindowProvider(frontmost=False)
    rc = RecoveryController(S.StratFile(base_dir="."), win, MockInputBackend(),
                            MockCaptureBackend(), MockComparator(), FakeClock(), mock_config())
    scene = Frame.labelled("whatever")  # matches no detector
    assert rc.classify(scene, live=True) == FailureMode.FOCUS_LOST
    assert rc.classify(scene, live=False) == FailureMode.NONE


def test_stuck_sync_not_misread_as_focus_lost():
    win = MockWindowProvider(frontmost=False)  # focus momentarily lost during a real stuck sync
    rc = RecoveryController(S.StratFile(base_dir="."), win, MockInputBackend(),
                            MockCaptureBackend(), MockComparator(), FakeClock(),
                            mock_config(max_attempts_per_cause=3))
    scene = Frame.labelled("frozen")  # not a known cause
    # must take the stuck-sync leave/reset path (REJOIN), NOT the FOCUS_LOST refocus path (RESUME)
    assert rc.handle(FailureMode.STUCK_SYNC, scene=scene) == Outcome.REJOIN
