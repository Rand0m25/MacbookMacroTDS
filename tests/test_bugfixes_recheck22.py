"""Regression tests for review round 22c (deep v5, third pass: 20 fixes).

The bulk are the long tail of hand-edited-JSON input validation. Several again refine my own fixes:
#1 (bool-for-float) mirrors round-22 #C (bool-for-int); #14/#15 refine round-22b's hotkeys work;
#7 refines round-22's per-event jitter feature."""

import json
import struct
import sys
import types
import zlib
from types import SimpleNamespace

import pytest

from tds_macro import strat as S
from tds_macro import cli
from tds_macro.config import Config, WindowBackendKind
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.visual import MockComparator
from tds_macro.window import (make_window_provider, MockWindowProvider, QuartzWindowProvider,
                              _GeometryOverrideProvider)
from tds_macro.recorder import Recorder
from tds_macro.recovery import RecoveryController
from tds_macro.launcher import MockLauncher
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.engine import RunState
from tds_macro.geometry import Point
from tds_macro.pngio import read_png
from tds_macro.errors import StratValidationError

from helpers import build_player, mock_config
from test_gui_controller import _setup


class _HK:
    def __init__(self):
        self.events = HotkeyEvents()


class _RecRng:
    def __init__(self):
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return 0.0


# --- #1 a bool override for a float field is rejected ---
def test_float_field_rejects_bool_override():
    with pytest.raises(ValueError):
        Config().with_overrides({"sync_default_threshold": True})


# --- #2 window_rect_override needs positive w/h ---
def test_window_rect_zero_size_rejected():
    assert any("window_rect_override" in p for p in Config(window_rect_override=(0, 0, 0, 0)).validate())
    assert not Config(window_rect_override=(0, 0, 100, 100)).validate()


# --- #3 a bad deflate stream surfaces as ValueError ---
def test_read_png_bad_idat(tmp_path):
    def _chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # valid 1x1 RGB header
    raw = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", b"\xff\xff\xff\xff") + _chunk(b"IEND", b"")
    p = tmp_path / "bad.png"
    p.write_bytes(raw)
    with pytest.raises(ValueError):
        read_png(str(p))


# --- #4 negative t_ms rejected (would reorder events) ---
def test_negative_t_ms_rejected():
    doc = {"events": [{"id": 1, "t_ms": -500, "type": "wait", "duration_ms": 0}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("t_ms" in p for p in ei.value.problems)


# --- #5 a non-string key is rejected at parse ---
def test_non_string_key_rejected():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "key_press", "key": ["a", "b"]}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("key" in p for p in ei.value.problems)


# --- #6/#9/#10 timeout_ms must be > 0 (sync_point, expect, run_end) ---
def test_sync_timeout_zero_rejected():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "a.png",
                       "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "timeout_ms": 0}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("timeout_ms" in p for p in ei.value.problems)


def test_run_end_zero_timeout_rejected():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [], "run_end": {"timeout_ms": 0}}, check_frames=False)
    assert any("timeout_ms" in p for p in ei.value.problems)


# --- #7 explicit per-event jitter_ms=0 suppresses global jitter; absent uses config ---
def test_explicit_zero_jitter_suppresses():
    st = S.StratFile(base_dir=".", events=[S.ClickEvent(1, 0, "click", "", 0, "left", Point(0.5, 0.5), 1, 0)])
    p, _, _, _ = build_player(st, cfg=mock_config(jitter_ms=50), clock=FakeClock())
    p._rng = _RecRng()
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert p._rng.calls == []   # explicit 0 -> no jitter despite global 50


def test_absent_jitter_uses_config():
    st = S.StratFile(base_dir=".", events=[S.ClickEvent(1, 0, "click", "", None, "left", Point(0.5, 0.5), 1, 0)])
    p, _, _, _ = build_player(st, cfg=mock_config(jitter_ms=50), clock=FakeClock())
    p._rng = _RecRng()
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert (-50, 50) in p._rng.calls


# --- #8 negative jitter_ms rejected ---
def test_negative_jitter_rejected():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "wait", "duration_ms": 0, "jitter_ms": -50}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("jitter_ms" in p for p in ei.value.problems)


# --- #11 recovery strips the relaunch URL before opening it ---
def test_relaunch_url_stripped():
    ml = MockLauncher()
    rc = RecoveryController(S.StratFile(base_dir="."), MockWindowProvider(), MockInputBackend(),
                            MockCaptureBackend(), MockComparator(), FakeClock(),
                            mock_config(relaunch_url="  roblox://x  "), launcher=ml)
    rc._relaunch_experience()
    assert ml.opened == ["roblox://x"]


# --- #12 sync_point isn't allowed in leave_reset_sequence (recovery silently drops it) ---
def test_sync_point_in_leave_reset_rejected():
    doc = {"events": [], "leave_reset_sequence": [
        {"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "a.png",
         "region": {"x": 0, "y": 0, "w": 1, "h": 1}}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("leave_reset_sequence" in p for p in ei.value.problems)


# --- #13 a pinned rect keeps REAL focus checks (not a focus-blind mock) ---
def test_quartz_rect_override_keeps_real_focus():
    prov = make_window_provider(Config(window_backend=WindowBackendKind.QUARTZ,
                                       window_rect_override=(0, 0, 800, 600)))
    assert isinstance(prov, _GeometryOverrideProvider)
    assert isinstance(prov._real, QuartzWindowProvider)   # real is_frontmost/activate
    assert prov.get_geometry().retina == 2.0              # geometry still pinned (recheck #w8.2)
    assert isinstance(make_window_provider(Config(window_backend=WindowBackendKind.MOCK,
                                                  window_rect_override=(0, 0, 800, 600))), MockWindowProvider)


# --- #14/#15 a non-ImportError pynput import failure still arms the kill-switch ---
def test_killswitch_armed_when_pynput_import_throws(tmp_path):
    m = types.ModuleType("pynput")

    def _ga(name):
        raise RuntimeError("no DISPLAY")

    m.__getattr__ = _ga
    hk = HotkeyManager(mock_config(killswitch_file=str(tmp_path / "ks")))
    old = sys.modules.get("pynput")
    sys.modules["pynput"] = m
    try:
        hk.start()
        assert hk._killswitch_thread is not None and hk._killswitch_thread.is_alive()
    finally:
        hk.stop()
        if old is not None:
            sys.modules["pynput"] = old
        else:
            sys.modules.pop("pynput", None)


# --- #16 capture_sync_point honors an explicit threshold=0.0 ---
def test_capture_sync_point_zero_threshold(tmp_path):
    import numpy as np

    from tds_macro.frame import Frame
    cap = MockCaptureBackend(frame_fn=lambda geo, region: Frame.from_numpy(np.zeros((4, 4, 4), dtype="uint8")))
    rec = Recorder(MockWindowProvider(frontmost=True, rect=(0, 0, 1600, 900)), MockInputBackend(),
                   cap, mock_config(frames_dir="frames"), hotkeys=None, clock=FakeClock())
    rec._strat_dir = str(tmp_path)
    rec._refresh_geo()
    sp = rec.capture_sync_point("s", threshold=0.0)
    assert sp.threshold == 0.0


# --- #17 the inert require_consent config knob is gone ---
def test_require_consent_field_removed():
    assert not hasattr(Config(), "require_consent")


# --- #18 GUI Validate exercises the same override coercion Play does ---
def test_gui_validate_catches_bad_overrides():
    st = S.StratFile(base_dir=".", config_overrides={"sync_match_method": "bogus"})

    def bc(**kw):
        return Config().with_overrides(kw.get("overrides") or {})

    ctrl, _, _ = _setup(load=lambda p: st, build_config=bc)
    ok, problems = ctrl.validate("x.json")
    assert ok is False and problems


# --- #19 cmd_calibrate now applies (and rejects bad) config_overrides ---
def test_cmd_calibrate_bad_overrides(tmp_path):
    doc = {"events": [], "config_overrides": {"sync_match_method": "bogus"}}
    p = tmp_path / "s.strat.json"
    p.write_text(json.dumps(doc))
    assert cli.cmd_calibrate(SimpleNamespace(strat=str(p), no_frames=True)) == 1


# --- #20 Ctrl-C during recording finalizes (saves) instead of discarding ---
def test_recorder_run_handles_keyboardinterrupt(tmp_path, monkeypatch):
    import tds_macro.recorder as rmod
    rec = Recorder(MockWindowProvider(frontmost=True, rect=(0, 0, 1600, 900)), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), hotkeys=_HK(), clock=FakeClock())

    def boom(*a):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rmod.time, "sleep", boom)
    st = rec.run(str(tmp_path / "s.json"))   # Ctrl-C in the loop -> still returns a built StratFile
    assert st is not None
