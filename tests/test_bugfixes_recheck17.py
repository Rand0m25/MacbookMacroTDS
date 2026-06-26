"""Regression tests for review round 19 (2 findings, both confirmed genuine):
  #1 engine._await_join: a long pause must not eat the join window (spurious WRONG_MAP)
  #2 recorder._t0: a legitimate 0.0 epoch must not be treated as 'unset' and rebased
"""

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recorder import Recorder
from tds_macro.hotkeys import HotkeyEvents

from helpers import build_player, mock_config
from test_gui_controller import FakeHK


class _StopHK:
    def __init__(self):
        self.events = HotkeyEvents()
        self.events.stop.set()  # makes run() exit its loop immediately


# --- #1 _await_join extends its deadline across a pause longer than join_timeout_ms ---
def test_await_join_extends_deadline_across_pause():
    hk = FakeHK()
    hk.events.pause.set()
    clk = FakeClock()

    def on_sleep(now):
        if now >= 1000:            # a pause far longer than join_timeout_ms (200)
            hk.events.pause.clear()

    clk.on_sleep = on_sleep
    st = S.StratFile(base_dir=".", events=[])  # no expected_map_check -> frontmost == joined
    p, _, _, _ = build_player(st, cfg=mock_config(join_timeout_ms=200, recovery_check_every_ms=10),
                              clock=clk, hotkeys=hk, window=MockWindowProvider(frontmost=True))
    # Without the deadline-extension the loop would exit right after the long pause and return
    # False (spurious WRONG_MAP); with it, the join completes once Roblox is frontmost.
    assert p._await_join() is True


# --- #2 a legitimate 0.0 epoch is preserved (is-None guard, not falsy) ---
def test_recorder_preserves_zero_epoch(tmp_path):
    rec = Recorder(MockWindowProvider(frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), hotkeys=_StopHK(), clock=FakeClock())
    rec._t0 = 0.0                # a pre-run capture_sync_point fired while the clock read 0.0
    rec.clock.advance(5000)
    rec.run(str(tmp_path / "s.json"))
    assert rec._t0 == 0.0        # NOT rebased to 5000 (the old `if not self._t0` boundary bug)


def test_recorder_now_ms_safe_before_epoch():
    # _now_ms must not crash if called before the epoch is seeded (_t0 is None)
    rec = Recorder(MockWindowProvider(frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), hotkeys=None, clock=FakeClock())
    assert rec._t0 is None
    assert rec._now_ms() == 0    # treats an unset epoch as origin 0, no TypeError
