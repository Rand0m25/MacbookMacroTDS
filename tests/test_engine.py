"""Engine: timeline order, monotonic clock stretch (M1), panic (M9/M10), loops, dry-run."""

from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.recovery import MockRecoveryController, Outcome, FailureMode
from tds_macro.window import MockWindowProvider
from tds_macro.engine import RunState, _RestartLoop
from tds_macro.frame import Frame
from tds_macro import strat as S
from tds_macro.geometry import Point

import pytest

from helpers import build_player, mock_config, mk_sync


# --- round 26 #1: a sync timeout repaired by focus-only recovery must RE-CONFIRM, not skip the gate ---
def test_focus_lost_sync_timeout_reconfirms_then_escalates():
    # sync never matches (ROI stays wrong); classify returns FOCUS_LOST first (recovery RESUMEs), then
    # NONE -> STUCK_SYNC -> REJOIN. The engine must NOT advance past the sync to dispatch the key.
    calls = {"n": 0}

    def classify_fn(frame):
        calls["n"] += 1
        return FailureMode.FOCUS_LOST if calls["n"] == 1 else FailureMode.NONE

    def handle_fn(reason, scene):
        return Outcome.RESUME if reason == FailureMode.FOCUS_LOST else Outcome.REJOIN

    rec = MockRecoveryController(handle_fn=handle_fn, classify_fn=classify_fn)
    events = [mk_sync(1, 0, "s1", on_timeout="recover", timeout=200),
              S.KeyPressEvent(2, 50, "key_press", key="a")]
    p, inp, _, _ = build_player(S.StratFile(base_dir=".", events=events), recovery=rec,
                                cfg=mock_config(sync_poll_ms=10), capture=MockCaptureBackend(current_label="nomatch"))
    with pytest.raises(_RestartLoop):  # re-confirm failed -> restart, rather than firing 'a' blind
        p._play_sequence(events, RunState.IN_MATCH, localize=True)
    assert not any(e["action"] == "key_press" for e in inp.events)  # the gated input never fired


# --- round 26 #2: a localize_on_start DECLINE must absorb the scan time so opening spacing is kept ---
class _TimedInput(MockInputBackend):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.key_times = {}

    def press_key(self, key, modifiers=()):
        super().press_key(key, modifiers)
        self.key_times[key] = self.clock.now_ms()


def test_localize_on_start_decline_preserves_opening_spacing():
    clock = FakeClock()

    def frame_fn(geo, region):
        clock.sleep(40)  # the localizer's grab+score scan burns wall time
        return Frame.labelled("nomatch")  # matches no checkpoint -> Hook A declines
    inp = _TimedInput(clock)
    events = [S.KeyPressEvent(1, 0, "key_press", key="a"),
              S.KeyPressEvent(2, 100, "key_press", key="b"),
              mk_sync(3, 10000, "s1", on_timeout="continue")]
    p, _, _, _ = build_player(S.StratFile(base_dir=".", events=events),
                              cfg=mock_config(localize_on_start=True),
                              capture=MockCaptureBackend(frame_fn=frame_fn), clock=clock, input_backend=inp)
    p._play_sequence(events, RunState.IN_MATCH, localize=True)
    assert inp.key_times["b"] - inp.key_times["a"] == 100  # recorded 100ms gap preserved after the scan


# --- RunStats.is_failure(): zero-match failures vs benign/successful stops ----------------
def test_runstats_is_failure():
    from tds_macro.engine import RunStats
    # benign / successful -> not a failure
    assert RunStats(runs=1, stopped_reason="loop_count reached").is_failure() is False
    assert RunStats(runs=0, stopped_reason="panic").is_failure() is False
    assert RunStats(runs=0, stopped_reason="session cap reached").is_failure() is False
    assert RunStats(runs=0, stopped_reason="").is_failure() is False
    assert RunStats(runs=1, stopped_reason="error: boom").is_failure() is False  # it DID complete a match
    # zero completed matches + a non-benign reason -> failure
    assert RunStats(runs=0, stopped_reason="error: WindowNotFoundError: x").is_failure() is True
    assert RunStats(runs=0, stopped_reason="recovery stopped on wrong_map").is_failure() is True
    assert RunStats(runs=0,
                    stopped_reason="aborted after 10 consecutive restarts without a completed run").is_failure() is True


# --- foreground validation: input must land in the Roblox window (verify_foreground) ----
_KEY = S.KeyPressEvent(1, 0, "key_press", key="a")
_WAIT = S.WaitEvent(1, 0, "wait", duration_ms=10)


def _fg_player(*, frontmost, handle_fn=None, **cfgkw):
    win = MockWindowProvider(frontmost=frontmost)
    rec = MockRecoveryController(handle_fn=handle_fn or (lambda r, sc: Outcome.RESUME))
    p, inp, _, _ = build_player(S.StratFile(base_dir="."), cfg=mock_config(**cfgkw),
                                window=win, recovery=rec)
    return p, inp, win, rec


def test_foreground_gate_skips_input_when_focus_unrecoverable():
    # not frontmost + recovery returns but can't refocus -> don't fire into the wrong app
    p, _, _, rec = _fg_player(frontmost=False)
    assert p._foreground_ok_for_input(_KEY) is False
    assert rec.handle_calls == [FailureMode.FOCUS_LOST] and p.stats.recoveries == 1


def test_foreground_gate_passes_after_refocus():
    def refocus(reason, scene):
        win.frontmost = True  # recovery brought Roblox back to the foreground
        return Outcome.RESUME
    p, _, win, _ = _fg_player(frontmost=False, handle_fn=refocus)
    assert p._foreground_ok_for_input(_KEY) is True


def test_foreground_gate_passes_when_already_frontmost():
    p, _, _, rec = _fg_player(frontmost=True)
    assert p._foreground_ok_for_input(_KEY) is True
    assert rec.handle_calls == []  # no recovery needed


def test_foreground_gate_bypassed_when_disabled():
    p, _, _, rec = _fg_player(frontmost=False, handle_fn=lambda r, sc: Outcome.STOP,
                              verify_foreground=False)
    assert p._foreground_ok_for_input(_KEY) is True  # opted out -> dispatch blind, no recovery
    assert rec.handle_calls == []


def test_foreground_gate_bypassed_in_dry_run():
    p, _, _, rec = _fg_player(frontmost=False, handle_fn=lambda r, sc: Outcome.STOP, dry_run=True)
    assert p._foreground_ok_for_input(_KEY) is True  # dry-run sends no input -> nothing to validate
    assert rec.handle_calls == []


def test_foreground_gate_ignores_non_input_events():
    p, _, _, rec = _fg_player(frontmost=False)
    assert p._foreground_ok_for_input(_WAIT) is True  # a wait sends nothing, so focus is irrelevant
    assert rec.handle_calls == []


def test_play_sequence_skips_input_without_focus():
    # end-to-end through _play_sequence: a key event is NOT dispatched while focus is lost
    p, inp, _, _ = _fg_player(frontmost=False)
    p._last_guard_ms = p.clock.now_ms()  # throttle the periodic guard so we isolate the per-input gate
    p._play_sequence([_KEY], RunState.IN_MATCH)
    assert not any(e["action"] == "key_press" for e in inp.events)


class TimedInput(MockInputBackend):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.key_times = {}

    def press_key(self, key, modifiers=()):
        super().press_key(key, modifiers)
        self.key_times[key] = self.clock.now_ms()


def _keys(*specs):
    return [S.KeyPressEvent(i + 1, t, "key_press", key=k) for i, (t, k) in enumerate(specs)]


def test_timeline_dispatch_order():
    st = S.StratFile(events=_keys((0, "a"), (100, "b"), (200, "c")), base_dir=".")
    p, inp, _, _ = build_player(st, cfg=mock_config(loop_count=1))
    p.run()
    order = [e["key"] for e in inp.events if e["action"] == "key_press"]
    assert order == ["a", "b", "c"]


def test_monotonic_clock_stretch_preserves_spacing():  # M1
    events = [S.KeyPressEvent(1, 0, "key_press", key="a"),
              mk_sync(2, 1000, "s1", stability_frames=1),
              S.KeyPressEvent(3, 1100, "key_press", key="b"),
              mk_sync(4, 2000, "s2", stability_frames=1),
              S.KeyPressEvent(5, 2100, "key_press", key="c")]
    st = S.StratFile(events=events, base_dir=".")
    cap = MockCaptureBackend(current_label="lobby")

    def on_sleep(now):
        cap.current_label = "lobby" if now < 4000 else ("s1" if now < 4600 else "s2")

    clk = FakeClock(on_sleep=on_sleep)
    inp = TimedInput(clk)
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=1), capture=cap, clock=clk, input_backend=inp)
    p.run()
    assert p.clock_offset >= 3000                      # stretched by the s1 lag
    assert inp.key_times["c"] - inp.key_times["b"] >= 999  # spacing preserved, not collapsed


def test_panic_halts_and_releases(monkeypatch):  # M9/M10/R22
    st = S.StratFile(events=[S.KeyPressEvent(1, 0, "key_press", key="a"),
                             mk_sync(2, 1000, "never")], base_dir=".")
    hk = HotkeyManager(mock_config(), HotkeyEvents())
    cap = MockCaptureBackend(current_label="nope")
    clk = FakeClock(on_sleep=lambda now: hk.events.panic.set() if now >= 2000 else None)
    p, inp, _, _ = build_player(st, cfg=mock_config(loop_count=1), capture=cap, clock=clk, hotkeys=hk)
    stats = p.run()
    assert stats.stopped_reason == "panic"
    assert not inp.held_keys  # release_all cleared the held "a"


def test_dry_run_injects_nothing():
    st = S.StratFile(events=[S.ClickEvent(1, 0, "click", pos=Point(0.5, 0.5)),
                             S.KeyPressEvent(2, 50, "key_press", key="x")], base_dir=".")
    p, inp, _, _ = build_player(st, cfg=mock_config(loop_count=1, dry_run=True))
    p.run()
    assert inp.events == []


def test_loop_count_runs_n_times():
    st = S.StratFile(events=_keys((0, "a")), base_dir=".")
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=3))
    stats = p.run()
    assert stats.runs == 3


def test_run_end_records_win():
    st = S.StratFile(
        events=_keys((0, "a")),
        run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=5000),
        base_dir=".")
    cap = MockCaptureBackend(current_label="victory")  # MockComparator matches det label "victory"
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=1), capture=cap)
    stats = p.run()
    assert stats.wins == 1


def test_expect_sync_timeout_classifies_out_of_cash():  # S12 / engine routing
    sp = mk_sync(1, 100, "expect_5", timeout=300, on_timeout="recover")
    st = S.StratFile(events=[sp], base_dir=".")
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.STOP)
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=1),
                              capture=MockCaptureBackend(current_label="nope"), recovery=rec)
    p.run()
    assert FailureMode.OUT_OF_CASH in rec.handle_calls


def test_sync_timeout_recover_restart_loop():
    st = S.StratFile(events=[mk_sync(1, 100, "never", timeout=500, on_timeout="recover")], base_dir=".")
    cap = MockCaptureBackend(current_label="nope")
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.REJOIN)
    # always-REJOIN recovery never completes a run; bounded by max_consecutive_restarts
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=1, max_consecutive_restarts=4),
                              capture=cap, recovery=rec)
    stats = p.run()
    assert stats.sync_timeouts >= 1 and rec.handle_calls
    assert stats.restarts == 4 and stats.runs == 0  # restart != completed run (R6)
