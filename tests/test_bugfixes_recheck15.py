"""Regression tests for review round 17 (4 findings, all confirmed genuine):
  #1 config._coerce_type: non-integral overrides for int fields (1.9 / "12.5")
  #2 engine.run: run_end configured but timed out must NOT count as a completed run
  #3 recorder.build: keys held at stop time must be paired with a synthetic release
  #4 gui.start_play: an explicit accept must start play even if consent can't persist
"""

import pytest

from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recorder import Recorder
from tds_macro.recovery import MockRecoveryController, FailureMode, Outcome

from helpers import build_player, mock_config
from test_gui_controller import _setup


# --- #1 int-field coercion is consistent: reject non-integral, accept integral ---
def test_coerce_int_rejects_nonintegral_float():
    with pytest.raises(ValueError) as ei:
        Config._coerce_type("loop_count", 1.9, 0)  # used to silently truncate to 1
    assert "loop_count" in str(ei.value)


def test_coerce_int_rejects_nonintegral_string():
    with pytest.raises(ValueError) as ei:
        Config._coerce_type("loop_count", "12.5", 0)  # used to crash with a raw int() error
    assert "loop_count" in str(ei.value)


def test_coerce_int_accepts_integral_forms():
    assert Config._coerce_type("loop_count", 5, 0) == 5
    assert Config._coerce_type("loop_count", "100", 0) == 100
    assert Config._coerce_type("loop_count", 5.0, 0) == 5   # integral float ok
    assert Config._coerce_type("loop_count", "12.0", 0) == 12


def test_coerce_int_bad_string_clear_error():
    with pytest.raises(ValueError) as ei:
        Config._coerce_type("loop_count", "abc", 0)
    assert "integer" in str(ei.value)


# --- #2 a configured run_end that times out is a stuck run, not a completed one ---
def test_run_end_timeout_not_counted_as_completed():
    st = S.StratFile(base_dir=".", events=[],
                     run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9),
                                      timeout_ms=50))
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.REJOIN)
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=5, max_consecutive_restarts=2,
                                                  recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="nomatch"),
                              clock=FakeClock(), recovery=rec,
                              window=MockWindowProvider(frontmost=True))
    stats = p.run()
    assert stats.runs == 0                          # phantom run not counted
    assert stats.wins == 0
    assert FailureMode.STUCK_SYNC in rec.handle_calls  # routed to recovery instead
    assert "consecutive restarts" in (stats.stopped_reason or "")


def test_no_run_end_still_completes():
    # When run_end is unconfigured, NONE-as-completion is intended and preserved.
    st = S.StratFile(base_dir=".",
                     events=[S.KeyPressEvent(1, 0, "key_press", key="a"),
                             S.KeyReleaseEvent(2, 0, "key_release", key="a")])
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=2),
                              clock=FakeClock(), window=MockWindowProvider(frontmost=True))
    stats = p.run()
    assert stats.runs == 2


# --- #3 a key still held when recording stops gets a synthetic release on build ---
def test_recorder_build_pairs_held_key(tmp_path):
    rec = Recorder(MockWindowProvider(frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), hotkeys=None, clock=FakeClock())
    rec._refresh_geo()
    rec._on_press("k")                       # press recorded; key now "held"
    assert "k" in rec._pressed_keys
    st = rec.build(str(tmp_path / "s.strat.json"))
    presses = [e for e in st.events if e.type == "key_press" and e.key == "k"]
    releases = [e for e in st.events if e.type == "key_release" and e.key == "k"]
    assert len(presses) == 1 and len(releases) == 1   # synthetic release paired the press
    assert not rec._pressed_keys                        # drained


# --- #4 explicit accept starts play even when consent persistence fails ---
def test_play_accept_overrides_failed_consent_persist():
    # set_consent can't write (simulates the swallowed OSError) so consent_ok stays False,
    # but an explicit accept this session must still let play start (no dead-end loop).
    ctrl, h, events = _setup(consent=False, set_consent=lambda: None)
    try:
        assert ctrl.start_play("x.json", accept_ban_risk=True) is True
        assert ("consent_required", None) not in events
    finally:
        ctrl.stop()
