"""Regression tests for review round 21.

3 of 4 findings confirmed genuine + fixed:
  #1 engine._play_sequence: jitter must not invert/collapse adjacent event spacing
  #3 config.looks_like_roblox_url: host is parsed, not substring-matched (URL-steering)
  #4 hotkeys.start: a listener failure (pynput present) logs a warning, not silent disable

#2 (_play_sequence abort-after-pause) was REJECTED as a false positive: every production
RealClock is built with should_abort=hk.should_abort, so sleep_until()'s entry _check() raises
PanicAbort before _dispatch_primitive — a panic while paused can't leak an event. See BUGLOG.
"""

import logging

import pytest

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.config import looks_like_roblox_url
from tds_macro.engine import RunState
from tds_macro.hotkeys import HotkeyManager

from helpers import build_player, mock_config


# --- #1 jitter never pulls an event before the previous one ---
class _RecClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.targets = []

    def sleep_until(self, deadline_ms):
        self.targets.append(deadline_ms)
        super().sleep_until(deadline_ms)


class _Rng:
    def __init__(self, vals):
        self.vals = list(vals)
        self.i = 0

    def uniform(self, a, b):
        v = self.vals[self.i % len(self.vals)]
        self.i += 1
        return v


def test_jitter_never_inverts_event_spacing():
    clk = _RecClock()
    st = S.StratFile(base_dir=".", events=[
        S.KeyPressEvent(1, 100, "key_press", key="a"),
        S.KeyPressEvent(2, 110, "key_press", key="b"),
    ])
    p, _, _, _ = build_player(st, cfg=mock_config(jitter_ms=50), clock=clk)
    p._rng = _Rng([50.0, -40.0])   # event1 +50 -> t=150; event2 -40 -> base t=70 (< 150)
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert clk.targets == sorted(clk.targets)   # monotonic: no back-to-back inversion
    assert clk.targets[1] >= clk.targets[0]


def test_jitter_zero_unchanged():
    clk = _RecClock()
    st = S.StratFile(base_dir=".", events=[
        S.KeyPressEvent(1, 100, "key_press", key="a"),
        S.KeyPressEvent(2, 110, "key_press", key="b"),
    ])
    p, _, _, _ = build_player(st, cfg=mock_config(jitter_ms=0), clock=clk)
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert clk.targets == [100.0, 110.0]   # no jitter -> exact recorded spacing


# --- #3 looks_like_roblox_url parses the host ---
@pytest.mark.parametrize("good", [
    "roblox://placeId=123",
    "roblox-player:1+launchmode",
    "https://www.roblox.com/games/123",
    "https://roblox.com/share?code=x",
    "https://ro.blox.com/abc",
    "https://share.ro.blox.com/abc",
    "http://roblox.com/games/1",
])
def test_url_accepts_real_roblox(good):
    assert looks_like_roblox_url(good) is True


@pytest.mark.parametrize("bad", [
    "https://roblox.com.evil.example/x",
    "https://evilroblox.com",
    "https://example.com/?ref=roblox.com",
    "https://notroblox.com",
    "http://roblox.com.attacker.net",
    "ftp://roblox.com",
    "roblox.com",   # no scheme
    "",
])
def test_url_rejects_impostors(bad):
    assert looks_like_roblox_url(bad) is False


# --- #4 a listener failure (pynput present) is logged, not silently swallowed ---
def test_hotkey_listener_failure_is_logged(caplog):
    import macfakes as F

    mods = F.make_pynput()
    def _boom(mapping):
        raise RuntimeError("simulated listener failure")
    mods["pynput.keyboard"].GlobalHotKeys = _boom
    hk = HotkeyManager(mock_config(killswitch_file=""))
    with F.installed(mods), caplog.at_level(logging.WARNING):
        assert hk.start() is False           # listener couldn't install
    hk.stop()
    assert "DISABLED" in caplog.text          # the failure is visible


def test_hotkey_missing_pynput_stays_quiet(caplog):
    # pynput isn't installed in this env -> ImportError path; expected, no DISABLED warning
    hk = HotkeyManager(mock_config(killswitch_file=""))
    with caplog.at_level(logging.WARNING):
        assert hk.start() is False
    hk.stop()
    assert "DISABLED" not in caplog.text
