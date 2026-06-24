"""Regression tests for review round 3 (see docs/BUGLOG.md).

Deliberately bounded (loop_count finite or panic pre-set) so a regression can
never hang the suite.
"""

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

from helpers import build_player, mock_config


def test_run_loop_is_panic_interruptible_with_empty_strat():
    # empty events + no run_end + loop_count=0 used to busy-spin un-interruptibly.
    st = S.StratFile(events=[], base_dir=".")
    hk = HotkeyManager(mock_config(), HotkeyEvents())
    hk.events.panic.set()  # pre-set: the top-of-loop abort check must catch it immediately
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=0), clock=FakeClock(), hotkeys=hk)
    stats = player.run()  # must return, not spin forever
    assert stats.stopped_reason == "panic"


def test_zero_work_iteration_sleeps_floor_not_busyspin():
    # bounded by loop_count=3 so it can't hang; assert the floor sleep actually ran.
    st = S.StratFile(events=[], base_dir=".")
    clk = FakeClock()
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=3, min_inter_event_ms=8), clock=clk)
    stats = player.run()
    assert stats.runs == 3
    # 3 iterations, each floor-sleeping ~8ms when they'd otherwise do zero work
    assert clk.now_ms() >= 2 * 8  # at least the between-iteration floor sleeps happened


def test_panic_midloop_via_sleep_terminates():
    # panic gets set during a floor sleep; the next top-of-loop check ends the run.
    st = S.StratFile(events=[], base_dir=".")
    hk = HotkeyManager(mock_config(), HotkeyEvents())
    clk = FakeClock(on_sleep=lambda now: hk.events.panic.set() if now >= 40 else None)
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=0, min_inter_event_ms=8),
                                   clock=clk, hotkeys=hk)
    stats = player.run()
    assert stats.stopped_reason == "panic"
    assert clk.now_ms() >= 40  # it slept (didn't busy-spin) until panic fired
