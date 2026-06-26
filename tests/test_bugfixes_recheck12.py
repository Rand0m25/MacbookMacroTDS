"""Regression tests for review round 14 (2 findings; see docs/BUGLOG.md)."""

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.engine import RunState
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

from helpers import build_player, mock_config


# #w-pause: after a pause resumes, state is restored (not left stuck on PAUSED)
def test_maybe_pause_restores_state():
    ev = HotkeyEvents()
    hk = HotkeyManager(mock_config(), ev)
    ev.pause.set()
    clk = FakeClock(on_sleep=lambda now: ev.pause.clear())  # clear the pause after the first tick
    p, _, _, _ = build_player(S.StratFile(base_dir="."), cfg=mock_config(), clock=clk, hotkeys=hk)
    p.state = RunState.IN_MATCH
    p._maybe_pause()
    assert p.state == RunState.IN_MATCH  # restored, not stuck on PAUSED


# #w-ks-join: stop() joins the killswitch watcher so it can't be orphaned by a later start()
def test_stop_joins_killswitch_thread(tmp_path):
    ks = str(tmp_path / "kill")
    hk = HotkeyManager(mock_config(killswitch_file=ks), HotkeyEvents())
    hk.start()
    t = hk._killswitch_thread
    assert t is not None and t.is_alive()
    hk.stop()
    assert hk._killswitch_thread is None
    assert not t.is_alive()  # fully torn down, not a leaked daemon


def test_restart_does_not_orphan_killswitch(tmp_path):
    ks = str(tmp_path / "kill")
    hk = HotkeyManager(mock_config(killswitch_file=ks), HotkeyEvents())
    hk.start()
    first = hk._killswitch_thread
    hk.start()  # restart without explicit stop -> guard must tear down the old watcher
    assert not first.is_alive()          # old watcher dead
    assert hk._killswitch_thread is not first and hk._killswitch_thread.is_alive()
    hk.stop()
