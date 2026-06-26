"""Regression tests for the 4th workflow recheck (3 findings; see docs/BUGLOG.md)."""

import os
import tempfile
import time

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents, _to_pynput_combo

from helpers import mock_config
import macfakes as F


# #1 a typo'd recovery key is reported (not silently dropping the detector)
def test_recovery_typo_key_reported():
    det = {"ref_frame": "f.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}}
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [], "recovery": {"wrng_map": det}}, check_frames=False)
    assert any("unknown field" in p and "recovery" in p for p in ei.value.problems)


# #3a hotkey combos are normalized (lowercased, empty tokens dropped)
def test_combo_normalization():
    assert _to_pynput_combo("F8") == "<f8>"
    assert _to_pynput_combo("Ctrl+Alt+P") == "<ctrl>+<alt>+p"
    assert _to_pynput_combo("") == ""
    assert _to_pynput_combo("ctrl+") == "<ctrl>"


# #3b one invalid hotkey must NOT take down the others (esp. panic)
def test_one_bad_hotkey_keeps_panic():
    with F.installed(F.make_pynput()):
        cfg = mock_config(panic_hotkey="f8", pause_hotkey="notakey",
                          start_hotkey="f9", mark_sync_hotkey="f10")
        hk = HotkeyManager(cfg, HotkeyEvents())
        try:
            assert hk.start() is True
            mapping = hk._listener.kwargs["mapping"]
            assert "<f8>" in mapping                          # panic survived
            assert not any("notakey" in k for k in mapping)   # bad pause skipped, not fatal
        finally:
            hk.stop()


# #2 killswitch watcher re-arms after a stop()/start() cycle on the same instance
def test_killswitch_rearms_after_restart():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    os.remove(path)  # not present at start
    hk = HotkeyManager(mock_config(killswitch_file=path), HotkeyEvents())
    try:
        hk.start()
        hk.stop()       # sets _killswitch_stop
        hk.start()      # must clear it and re-arm the watcher
        time.sleep(0.35)
        open(path, "w").close()  # trigger
        time.sleep(0.35)
        assert hk.events.panic.is_set(), "killswitch watcher did not re-arm after restart"
    finally:
        hk.stop()
        if os.path.exists(path):
            os.remove(path)
