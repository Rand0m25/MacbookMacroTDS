"""Adaptive visual-sync barrier: fire-on-match, stability, M2 timeout decoupling, edge gate."""

from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.engine import WaitResult
from tds_macro import strat as S

from helpers import build_player, mock_config, mk_sync


def _player(cap, clk, **cfgkw):
    st = S.StratFile(events=[], base_dir=".")
    p, _, _, _ = build_player(st, cfg=mock_config(**cfgkw), capture=cap, clock=clk)
    return p


def test_fires_on_match():
    cap = MockCaptureBackend(current_label="s")
    p = _player(cap, FakeClock())
    res, _ = p._adaptive_wait(mk_sync(1, 0, "s", stability_frames=1))
    assert res == WaitResult.FIRE


def test_requires_stability_frames():
    cap = MockCaptureBackend(current_label="s")
    clk = FakeClock()
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", stability_frames=4)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE
    # 4 consecutive matches => 3 inter-poll sleeps of 100ms
    assert clk.now_ms() >= 300


def test_timeout_decoupled_from_stability():  # M2
    # tiny timeout but a big stability window: must NOT spuriously time out on a real match
    cap = MockCaptureBackend(current_label="s")
    clk = FakeClock()
    p = _player(cap, clk, sync_timeout_slack_ms=500)
    sync = mk_sync(1, 0, "s", stability_frames=5, timeout=50)  # 50ms << stability window
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # would be TIMEOUT without the M2 auto-bump


def test_timeout_when_never_matches():
    cap = MockCaptureBackend(current_label="nope")
    clk = FakeClock()
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", timeout=1000)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.TIMEOUT
    assert clk.now_ms() >= 1000


def test_rising_edge_gate_needs_transition():  # S2
    # require_settled => must see a non-match before accepting; an always-matching
    # (lingering previous-loop) screen must NOT instantly satisfy the sync.
    cap = MockCaptureBackend(current_label="s")
    clk = FakeClock()
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", timeout=800, require_settled=True)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.TIMEOUT  # no edge -> never fires


def test_rising_edge_fires_after_transition():  # S2
    cap = MockCaptureBackend(current_label="nope")
    clk = FakeClock(on_sleep=lambda now: setattr(cap, "current_label", "s") if now >= 300 else None)
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", timeout=5000, require_settled=True, stability_frames=1)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # saw non-match, then match -> edge satisfied
