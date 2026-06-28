"""Recovery FSM: classify, totality of handle (M4), per-cause caps (M5), M17 vocab."""

from tds_macro.clock import FakeClock
from tds_macro.window import MockWindowProvider
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.visual import MockComparator
from tds_macro.recovery import RecoveryController, FailureMode, Outcome
from tds_macro.frame import Frame
from tds_macro.geometry import Point, Rect
from tds_macro import strat as S

from helpers import mock_config


def _controller(strat=None, window=None, cfg=None):
    strat = strat or S.StratFile(events=[], base_dir=".")
    return RecoveryController(strat, window or MockWindowProvider(), MockInputBackend(),
                              MockCaptureBackend(), MockComparator(), FakeClock(),
                              cfg or mock_config())


def test_classify_detects_disconnect():
    st = S.StratFile(events=[], recovery=S.RecoverySpec(
        disconnect=S.DetectorSpec("dc.png", Rect(0, 0, 1, 1), 0.9)), base_dir=".")
    rc = _controller(st)
    assert rc.classify(Frame.labelled("dc.png")) == FailureMode.DISCONNECTED
    assert rc.classify(Frame.labelled("something_else")) == FailureMode.NONE
    assert rc.classify(None) == FailureMode.STUCK_SYNC


def test_focus_lost_does_not_activate_in_dry_run():  # round 26 #4
    win = MockWindowProvider(frontmost=False)
    rc = _controller(window=win, cfg=mock_config(dry_run=True))
    assert rc.handle(FailureMode.FOCUS_LOST) == Outcome.RESUME
    assert win.activate_calls == 0  # a preview must not yank focus to Roblox


def test_reconnect_confirms_lobby_after_a_delay():  # round 26 #3
    # the hub becomes visible only after a couple of polls; the bounded confirm window must catch it
    # (a single immediate check would miss it -> per-cause budget never resets -> premature STOP).
    st = S.StratFile(events=[], base_dir=".", recovery=S.RecoverySpec(
        disconnect=S.DetectorSpec("dc.png", Rect(0, 0, 1, 1), 0.9),
        lobby_anchor=S.DetectorSpec("hub.png", Rect(0, 0, 1, 1), 0.9)))
    calls = {"n": 0}

    def frame_fn(geo, region):
        calls["n"] += 1
        return Frame.labelled("hub.png" if calls["n"] >= 3 else "loading")  # hub appears on the 3rd grab
    rc = RecoveryController(st, MockWindowProvider(), MockInputBackend(), MockCaptureBackend(frame_fn=frame_fn),
                            MockComparator(), FakeClock(), mock_config(join_timeout_ms=1000, recovery_check_every_ms=10))
    assert rc.handle(FailureMode.DISCONNECTED) == Outcome.REJOIN
    assert rc.attempts.get("disconnected", 0) == 0  # confirmed reach of the hub -> per-cause counter reset


def test_handle_is_total_over_failure_modes():  # M4
    rc = _controller(cfg=mock_config(max_attempts_per_cause=99))
    for fm in FailureMode:
        out = rc.handle(fm, scene=Frame.labelled("x"))
        assert isinstance(out, Outcome)


def test_per_cause_cap_stops(monkeypatch):  # M5
    rc = _controller(cfg=mock_config(max_attempts_per_cause=3))
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.STOP  # 4th over cap


def test_disconnect_reconnects_not_resets():  # M17
    inp = MockInputBackend()
    rc = RecoveryController(S.StratFile(events=[], base_dir="."), MockWindowProvider(), inp,
                            MockCaptureBackend(), MockComparator(), FakeClock(), mock_config())
    out = rc.handle(FailureMode.DISCONNECTED)
    keys = [e.get("key") for e in inp.events if "key" in e]
    assert out == Outcome.REJOIN
    assert "enter" in keys and "esc" not in keys  # reconnect, never leave/reset on a disconnect


def test_wrong_map_runs_leave_reset_sequence():
    seq = [S.ClickEvent(1, 0, "click", pos=Point(0.5, 0.62), comment="Leave")]
    st = S.StratFile(events=[], leave_reset_sequence=seq, base_dir=".")
    inp = MockInputBackend()
    rc = RecoveryController(st, MockWindowProvider(), inp, MockCaptureBackend(), MockComparator(),
                            FakeClock(), mock_config())
    out = rc.handle(FailureMode.WRONG_MAP)
    assert out == Outcome.REJOIN
    assert any(e.get("key") == "esc" for e in inp.events)        # Roblox menu opened
    assert any(e["action"] == "click" for e in inp.events)       # recorded Leave click ran


def test_lobby_anchor_confirmation_resets_budget():
    # confirmed return to hub -> budget resets -> self-heals indefinitely (plan R17/§8.5)
    st = S.StratFile(events=[], leave_reset_sequence=[S.ClickEvent(1, 0, "click", pos=Point(0.5, 0.6))],
                     recovery=S.RecoverySpec(lobby_anchor=S.DetectorSpec("lobby.png", Rect(0, 0, 1, 1), 0.9)),
                     base_dir=".")
    cap = MockCaptureBackend(current_label="lobby.png")  # screen shows the hub
    rc = RecoveryController(st, MockWindowProvider(), MockInputBackend(), cap, MockComparator(),
                            FakeClock(), mock_config(max_attempts_per_cause=2))
    for _ in range(5):
        assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN  # never STOPs


def test_lobby_anchor_unconfirmed_hits_cap():
    st = S.StratFile(events=[], recovery=S.RecoverySpec(
        lobby_anchor=S.DetectorSpec("lobby.png", Rect(0, 0, 1, 1), 0.9)), base_dir=".")
    cap = MockCaptureBackend(current_label="still_in_match")  # never confirms hub
    rc = RecoveryController(st, MockWindowProvider(), MockInputBackend(), cap, MockComparator(),
                            FakeClock(), mock_config(max_attempts_per_cause=2))
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.REJOIN
    assert rc.handle(FailureMode.WRONG_MAP) == Outcome.STOP  # unconfirmed retries hit the cap


def test_focus_lost_refocuses():
    win = MockWindowProvider(frontmost=False)
    rc = _controller(window=win)
    out = rc.handle(FailureMode.FOCUS_LOST)
    assert win.activate_calls == 1 and out == Outcome.RESUME


def test_stuck_sync_reclassifies_to_specific():
    st = S.StratFile(events=[], recovery=S.RecoverySpec(
        disconnect=S.DetectorSpec("dc.png", Rect(0, 0, 1, 1), 0.9)), base_dir=".")
    inp = MockInputBackend()
    rc = RecoveryController(st, MockWindowProvider(), inp, MockCaptureBackend(), MockComparator(),
                            FakeClock(), mock_config())
    # scene looks like a disconnect -> stuck reclassifies and reconnects (enter)
    out = rc.handle(FailureMode.STUCK_SYNC, scene=Frame.labelled("dc.png"))
    assert out == Outcome.REJOIN
    assert any(e.get("key") == "enter" for e in inp.events)
