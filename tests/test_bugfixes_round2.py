"""Regression tests for review round 2 (see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError, WindowNotFoundError
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.geometry import WindowGeometry

from helpers import build_player, mock_config


# --- strat: non-dict containers collected, not crashing (R2 D1/D2) ---
@pytest.mark.parametrize("doc,needle", [
    ({"events": [], "config_overrides": "nope"}, "config_overrides must be an object"),
    ({"events": [], "config_overrides": [1, 2]}, "config_overrides must be an object"),
    ({"events": [], "recovery": ["wrong_map"]}, "recovery must be an object"),
    ({"events": [], "recovery": "x"}, "recovery must be an object"),
])
def test_non_dict_container_reported(doc, needle):
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any(needle in p for p in ei.value.problems)


def test_valid_config_overrides_still_work():
    st = S.parse({"events": [], "config_overrides": {"loop_count": 5}}, check_frames=False)
    assert st.config_overrides["loop_count"] == 5


# --- engine R2 D3: join_sequence and events keep independent spacing ---
class _TimedInput(MockInputBackend):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.key_times = {}

    def press_key(self, key, modifiers=()):
        super().press_key(key, modifiers)
        self.key_times[key] = self.clock.now_ms()


def test_join_then_events_spacing_preserved():
    join = [S.KeyPressEvent(1, 0, "key_press", key="j"),
            S.WaitEvent(2, 2000, "wait", duration_ms=2000)]
    events = [S.KeyPressEvent(1, 0, "key_press", key="a"),
              S.KeyPressEvent(2, 500, "key_press", key="b")]
    st = S.StratFile(events=events, join_sequence=join, base_dir=".")
    clk = FakeClock()
    inp = _TimedInput(clk)
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=1), clock=clk, input_backend=inp)
    player.run()
    # without the per-sequence rebase, a and b would both fire at t=2000 (0ms apart)
    assert inp.key_times["b"] - inp.key_times["a"] >= 499
    assert inp.key_times["a"] >= 2000  # events start AFTER the join finished


# --- recorder R2 D4: scroll gated on frontmost ---
def test_scroll_dropped_when_not_frontmost():
    from tds_macro.recorder import Recorder
    win = MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=False)
    rec = Recorder(win, MockInputBackend(), MockCaptureBackend(), mock_config(),
                   HotkeyManager(mock_config(), HotkeyEvents()), clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    rec._on_scroll(500, 500, 0, 3)
    assert rec.coalescer.finish() == []


# --- engine R2 D5: unexpected error -> graceful stop, inputs released, reason recorded ---
class _FlakyWindow:
    def __init__(self):
        self.calls = 0

    def get_geometry(self):
        self.calls += 1
        if self.calls == 1:
            return WindowGeometry(0, 0, 100, 100)  # construction succeeds
        raise WindowNotFoundError("window vanished")  # then it disappears

    def is_frontmost(self):
        return True

    def activate(self):
        pass


def test_unexpected_error_is_graceful():
    st = S.StratFile(events=[S.KeyPressEvent(1, 0, "key_press", key="a")], base_dir=".")
    inp = MockInputBackend()
    player, _, _, _ = build_player(st, cfg=mock_config(loop_count=1), window=_FlakyWindow(),
                                   input_backend=inp)
    stats = player.run()  # must NOT raise
    assert stats.stopped_reason.startswith("error:")
    assert not inp.held_keys  # release_all still ran in finally
