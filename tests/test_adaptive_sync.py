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


def test_require_settled_fires_on_stable_match():  # S2 / recheck #w2.2
    # require_settled now means "wait for the screen to settle", NOT "require a
    # transition". An already-stable matching screen must FIRE, not spuriously
    # time out into recovery (the strict rising-edge hang was a bug).
    cap = MockCaptureBackend(current_label="s")
    clk = FakeClock()
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", timeout=800, require_settled=True, stability_frames=1)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # already stable + matching -> fires (no spurious timeout)


def test_require_settled_runs_a_settle_check_even_at_stability_one():  # recheck #w-settle
    # require_settled must not fire on the bare first frame (no settle comparison possible);
    # it fires only after >=1 frame-to-frame settle check, even when stability_frames==1.
    cap = MockCaptureBackend(current_label="s")
    clk = FakeClock()
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", require_settled=True, stability_frames=1, timeout=5000)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE
    assert clk.now_ms() >= 100  # advanced >=1 poll -> a settle check actually ran

    # contrast: a plain sync (no require_settled) fires on the very first frame
    clk2 = FakeClock()
    p2 = _player(MockCaptureBackend(current_label="s"), clk2)
    s2 = mk_sync(2, 0, "s", stability_frames=1, timeout=5000)
    s2.poll_ms = 100
    r2, _ = p2._adaptive_wait(s2)
    assert r2 == WaitResult.FIRE and clk2.now_ms() == 0


def test_rising_edge_fires_after_transition():  # S2
    cap = MockCaptureBackend(current_label="nope")
    clk = FakeClock(on_sleep=lambda now: setattr(cap, "current_label", "s") if now >= 300 else None)
    p = _player(cap, clk)
    sync = mk_sync(1, 0, "s", timeout=5000, require_settled=True, stability_frames=1)
    sync.poll_ms = 100
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # saw non-match, then match -> edge satisfied
