"""Regression tests for the feature-review round (4 raised; #2 grab_region retina was a
verified false positive — mss CGWindowListCreateImage takes points; see docs/BUGLOG.md)."""

import pytest

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.launcher import MockLauncher

from helpers import build_player, mock_config

URL = "roblox://placeId=1&linkCode=z"


# #w-dry: a dry-run start must NOT open the link or stall on the join wait
def test_dry_run_join_does_not_open_or_stall():
    ml = MockLauncher()
    st = S.StratFile(base_dir=".", header=S.Header(private_server_url=URL),
                     expected_map_check=S.DetectorSpec("map.png", S.Rect(0, 0, 1, 1), 0.9))
    clk = FakeClock()
    p, _, _, _ = build_player(st, cfg=mock_config(dry_run=True, join_timeout_ms=30000,
                                                  recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="never"),
                              launcher=ml, clock=clk)
    p._join()
    assert ml.opened == []     # dry-run never opened the link
    assert clk.now_ms() == 0   # never polled/stalled for join_timeout -> no forced recovery


# #w-click: a pynput click that raises mid-call leaves the button TRACKED so release_all recovers it
def test_pynput_click_leaves_button_tracked_on_failure():
    import macfakes as F
    with F.installed(F.make_pynput()):
        from tds_macro.input_backend import PynputInputBackend

        pib = PynputInputBackend(); pib._ensure()

        class BoomClick:
            position = (0.0, 0.0)
            def click(self, b, n): raise RuntimeError("boom")

        pib._mouse = BoomClick()
        with pytest.raises(RuntimeError):
            pib.click("left", 0.0, 0.0, clicks=2)  # double-click path
        assert "left" in pib._held_buttons

        pib2 = PynputInputBackend(); pib2._ensure()

        class BoomRelease:
            position = (0.0, 0.0)
            def press(self, b): pass
            def release(self, b): raise RuntimeError("rel")

        pib2._mouse = BoomRelease()
        with pytest.raises(RuntimeError):
            pib2.click("left", 0.0, 0.0, clicks=1, hold_ms=0)  # single-click path
        assert "left" in pib2._held_buttons


# #w-dblstart: start() twice without stop() must tear down the previous listener
def test_hotkeys_double_start_tears_down_old_listener():
    import macfakes as F
    from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
    with F.installed(F.make_pynput()):
        hk = HotkeyManager(mock_config(), HotkeyEvents())
        assert hk.start()
        first = hk._listener
        assert first is not None
        hk.start()  # double start
        assert first.stopped is True       # old OS listener torn down (no leak)
        assert hk._listener is not first   # replaced by a fresh one
        hk.stop()
