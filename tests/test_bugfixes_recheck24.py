"""Regression tests for review round 23 (post-systematic-pass: 9 fixes; #4 — the focus-loss
deadline-extension cap — was REJECTED as a deliberate anti-hang trade-off with a bounded safe
outcome). Several refine my own fixes: #7 (round-22 refcount), #9 (round-18/22 permission check),
#10/#2 (the GUI/leave_reset validation gaps)."""

import logging

import pytest

import macfakes as F
from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.recovery import MockRecoveryController, FailureMode
from tds_macro.errors import StratValidationError

from helpers import build_player, mock_config, mk_sync
from test_gui_controller import _setup


# --- #1 scroll deltas get a sane cap (but may be negative) ---
def test_scroll_delta_bounded():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [{"id": 1, "t_ms": 0, "type": "scroll", "dx": 1000000, "dy": 0}]}, check_frames=False)
    assert any("dx" in p for p in ei.value.problems)
    st = S.parse({"events": [{"id": 1, "t_ms": 0, "type": "scroll", "dx": -5, "dy": 3}]}, check_frames=False)
    assert [e for e in st.events if e.type == "scroll"][0].dx == -5   # negative scroll still ok


# --- #2 a leave_reset macro carrying 'expect' (expands to a sync) is rejected ---
def test_leave_reset_macro_with_expect_rejected():
    doc = {"events": [], "leave_reset_sequence": [
        {"id": 1, "t_ms": 0, "type": "place_tower", "tower": "x", "hotbar_slot": 1,
         "pos": {"x": 0.5, "y": 0.5},
         "expect": {"ref_frame": "v.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}}}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("leave_reset_sequence" in p for p in ei.value.problems)


# --- #3 a stuck sync that's really the win screen counts as a win, not a restart ---
def test_stuck_sync_victory_counts_as_win():
    rec = MockRecoveryController(classify_fn=lambda f: FailureMode.VICTORY)
    st = S.StratFile(base_dir=".", events=[mk_sync(1, 0, "wave1", timeout=50, on_timeout="recover")])
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=1, recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="nomatch"),
                              clock=FakeClock(), recovery=rec)
    stats = p.run()
    assert stats.wins == 1 and stats.runs == 1 and stats.restarts == 0


# --- #6 a missing lobby_anchor (with other detectors) warns ---
def test_missing_lobby_anchor_warns(caplog):
    doc = {"events": [], "recovery": {"disconnect": {"ref_frame": "d.png",
                                                     "region": {"x": 0, "y": 0, "w": 1, "h": 1}}}}
    with caplog.at_level(logging.WARNING):
        S.parse(doc, check_frames=False)
    assert "lobby_anchor" in caplog.text


# --- #7 a release that raises leaves the key tracked for release_all recovery ---
def test_release_key_keeps_tracked_on_raise():
    with F.installed(F.make_pynput()):
        from tds_macro.input_backend import PynputInputBackend
        b = PynputInputBackend()
        b._ensure()
        b.press_key("a")

        class _Boom:
            def press(self, k):
                pass

            def release(self, k):
                raise RuntimeError("boom")

        b._kb = _Boom()
        b.release_key("a")   # OS release raises -> 'a' must remain tracked, not become stuck
        assert "a" in b._held_keys


# --- #8 hold_ms is capped (can't block panic for minutes) ---
def test_hold_ms_capped():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "click", "pos": {"x": 0.5, "y": 0.5}, "hold_ms": 60000}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("hold_ms" in p for p in ei.value.problems)


# --- #9 (HIGH) an OPAQUE black frame (real macOS denial) is detected as denied ---
def test_screen_recording_opaque_black_is_denied(monkeypatch):
    import numpy as np

    from tds_macro import permissions
    from tds_macro.frame import Frame
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    arr = np.zeros((100, 100, 4), dtype="uint8")
    arr[:, :, 3] = 255   # RGB(0,0,0) but alpha=255 — what a real denied capture looks like

    class Cap:
        def grab_window(self, geo):
            return Frame.from_numpy(arr)

    assert permissions.check_screen_recording(Cap(), geo=object()) is False


# --- #10 GUI Validate now also vets the typed private-server link ---
def test_gui_validate_checks_link():
    st = S.StratFile(base_dir=".")

    def bc(**kw):
        c = Config()
        if kw.get("private_server"):
            c.private_server_url = kw["private_server"]
        return c

    ctrl, _, _ = _setup(load=lambda p: st, build_config=bc)
    ok, problems = ctrl.validate("x.json", private_server="https://evil.com")
    assert ok is False and any("roblox" in p.lower() for p in problems)
