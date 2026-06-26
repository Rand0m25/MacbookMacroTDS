"""Regression tests for the 3rd workflow recheck (3 findings; see docs/BUGLOG.md)."""

import os
import tempfile
import threading
import time

from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

from helpers import build_player, mock_config


# #1 retina_scale_override <= 0 is rejected by validate()
def test_retina_override_nonpositive_rejected():
    assert any("retina_scale_override" in p
               for p in Config().with_overrides({"retina_scale_override": 0.0}).validate())
    assert any("retina_scale_override" in p
               for p in Config().with_overrides({"retina_scale_override": -2.0}).validate())
    assert Config().with_overrides({"retina_scale_override": 2.0}).validate() == []


# #2a recovery_check_every_ms <= 0 is rejected by validate()
def test_recovery_check_every_ms_nonpositive_rejected():
    assert any("recovery_check_every_ms" in p
               for p in Config().with_overrides({"recovery_check_every_ms": 0}).validate())


# #2b even if forced to 0, _wait_run_end terminates (floored sleep), never hangs/busy-spins
def test_wait_run_end_terminates_with_zero_interval():
    st = S.StratFile(events=[], run_end=S.RunEnd(
        defeat=S.DetectorSpec("defeat.png", S.Rect(0, 0, 1, 1), 0.9), timeout_ms=80), base_dir=".")
    cfg = mock_config(recovery_check_every_ms=0)  # forced bad value (bypassing validate)
    p, _, _, _ = build_player(st, cfg=cfg, clock=FakeClock(),
                              capture=MockCaptureBackend(current_label="nope"))
    done = {}
    t = threading.Thread(target=lambda: done.update(fm=p._wait_run_end()), daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive(), "_wait_run_end hung with recovery_check_every_ms=0"
    assert "fm" in done


# #3 killswitch: a stale pre-existing file must NOT instant-panic; a NEW one must
def test_killswitch_ignores_stale_file_but_catches_new():
    fd, path = tempfile.mkstemp()
    os.close(fd)  # the file exists BEFORE start (stale)
    hk = HotkeyManager(mock_config(killswitch_file=path), HotkeyEvents())
    try:
        hk.start()  # should delete the stale file
        time.sleep(0.35)
        assert not hk.events.panic.is_set(), "stale killswitch file caused a false panic"
        assert not os.path.exists(path), "stale killswitch file should have been cleared"
        open(path, "w").close()  # NOW create it (the documented trigger)
        time.sleep(0.35)
        assert hk.events.panic.is_set(), "a newly-created killswitch file should trigger panic"
    finally:
        hk.stop()
        if os.path.exists(path):
            os.remove(path)
