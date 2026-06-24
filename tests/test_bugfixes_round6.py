"""Regression tests for review round 6 (see docs/BUGLOG.md)."""

import json

import pytest

from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.errors import StratValidationError
from tds_macro.capture import MockCaptureBackend
from tds_macro.recovery import MockRecoveryController, Outcome

from helpers import build_player, mock_config, mk_sync


# 1) Config.validate is wired into cmd_play -> bad override rejected, not busy-spin
def test_cmd_play_rejects_invalid_config(tmp_path):
    from tds_macro.cli import build_parser
    p = tmp_path / "s.strat.json"
    p.write_text(json.dumps({"events": [], "config_overrides": {"sync_poll_ms": 0}}))
    args = build_parser().parse_args(
        ["play", str(p), "--mock", "--dry-run", "--no-frames", "--accept-ban-risk"])
    assert args.func(args) == 1  # invalid config -> exit 1, never reaches the busy-spin


# 2) _coerce_type coerces string/bool overrides (and rejects junk)
def test_coerce_type_numeric_and_bool():
    c = Config().with_overrides({"sync_poll_ms": "100", "loop_count": "3", "dry_run": "true",
                                 "sync_default_threshold": "0.8"})
    assert c.sync_poll_ms == 100 and isinstance(c.sync_poll_ms, int)
    assert c.loop_count == 3
    assert c.dry_run is True
    assert c.sync_default_threshold == 0.8


@pytest.mark.parametrize("bad", [{"sync_poll_ms": "abc"}, {"dry_run": "maybe"},
                                 {"sync_default_threshold": "lots"}])
def test_coerce_type_rejects_junk(bad):
    with pytest.raises(ValueError):
        Config().with_overrides(bad)


# 3) UpgradeEvent.times is bounded
def test_upgrade_times_bounded():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"type": "upgrade", "id": 1, "t_ms": 0,
                             "target_pos": {"x": 0.5, "y": 0.5},
                             "upgrade_button_pos": {"x": 0.9, "y": 0.7}, "times": 9999}]},
                check_frames=False)
    assert any("times" in p and "1..50" in p for p in ei.value.problems)


# 4) a restart is NOT a completed run, releases input, and is bounded
def test_restart_semantics_and_release():
    events = [S.KeyPressEvent(1, 0, "key_press", key="a"),
              mk_sync(2, 50, "never", timeout=200, on_timeout="recover")]
    st = S.StratFile(events=events, base_dir=".")
    rec = MockRecoveryController(handle_fn=lambda r, sc: Outcome.REJOIN)  # never STOPs
    player, inp, _, _ = build_player(
        st, cfg=mock_config(loop_count=0, max_consecutive_restarts=3),
        capture=MockCaptureBackend(current_label="nope"), recovery=rec)
    stats = player.run()
    assert stats.runs == 0                 # never completed -> doesn't satisfy loop_count
    assert stats.restarts == 3             # bounded by max_consecutive_restarts
    assert "consecutive restarts" in stats.stopped_reason
    assert not inp.held_keys               # input released across restarts + final


# 5) recorder ignores OS key auto-repeat
def test_recorder_ignores_key_autorepeat():
    from tds_macro.recorder import Recorder
    from tds_macro.window import MockWindowProvider
    from tds_macro.input_backend import MockInputBackend
    from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
    from tds_macro.clock import FakeClock
    win = MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True)
    rec = Recorder(win, MockInputBackend(), MockCaptureBackend(), mock_config(),
                   HotkeyManager(mock_config(), HotkeyEvents()), clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    rec._on_press("a")   # real press
    rec._on_press("a")   # OS auto-repeat tick
    rec._on_press("a")   # OS auto-repeat tick
    rec._on_release("a")
    evs = rec.coalescer.finish()
    assert [e.type for e in evs] == ["key_press", "key_release"]  # one press, not three
