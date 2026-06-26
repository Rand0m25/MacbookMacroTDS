"""Regression tests for review round 22 (deep v5 round: 18 fixes; finding K — recovery _match
not cropping to det.region — was REJECTED as the deliberate D9 full-frame-classify design).

Several of these guard regressions/gaps from THIS session's own fixes (A: round-21 urlparse;
O: round-18 capture clamp; P: round-17 held-key drain; I/S/R: the GUI feature)."""

import struct
import time
import zlib

import pytest

import macfakes as F
from tds_macro import strat as S
from tds_macro.config import Config, looks_like_roblox_url
from tds_macro.pngio import read_png
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.window import MockWindowProvider
from tds_macro.visual import MockComparator
from tds_macro.recorder import Recorder
from tds_macro.recovery import RecoveryController, FailureMode, Outcome
from tds_macro.hotkeys import HotkeyEvents, HotkeyManager
from tds_macro.engine import RunState
from tds_macro.geometry import Point
from tds_macro.errors import StratValidationError

from helpers import build_player, mock_config
from test_gui_controller import _setup


# --- #A looks_like_roblox_url is total (no ValueError on a malformed host) ---
def test_url_malformed_ipv6_is_false_not_crash():
    assert looks_like_roblox_url("http://[::1") is False   # urlparse would raise -> must be caught
    assert looks_like_roblox_url("https://[::1]/x") is False
    assert looks_like_roblox_url("https://www.roblox.com/x") is True


# --- #B relaunch_url is validated like private_server_url ---
def test_relaunch_url_validated():
    assert any("relaunch_url" in p for p in Config(relaunch_url="file:///etc/passwd").validate())
    assert not any("relaunch_url" in p for p in Config(relaunch_url="roblox://x").validate())


# --- #C an int config field rejects a bool override (not silently 1) ---
def test_int_field_rejects_bool_override():
    with pytest.raises(ValueError):
        Config().with_overrides({"loop_count": True})
    assert Config().with_overrides({"loop_count": 3}).loop_count == 3   # real int still fine


# --- #D a bad IHDR length raises a clean ValueError, not struct.error ---
def test_read_png_bad_ihdr_length(tmp_path):
    data = b"\x00" * 5  # IHDR must be 13 bytes
    chunk = struct.pack(">I", 5) + b"IHDR" + data + struct.pack(">I", zlib.crc32(b"IHDR" + data) & 0xFFFFFFFF)
    p = tmp_path / "bad.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)
    with pytest.raises(ValueError) as ei:
        read_png(str(p))
    assert "IHDR" in str(ei.value)


# --- #E click count is bounded (no hand-edited click storm) ---
def _click_doc(clicks):
    return {"events": [{"id": 1, "t_ms": 0, "type": "click", "pos": {"x": 0.5, "y": 0.5}, "clicks": clicks}]}


@pytest.mark.parametrize("bad", [100000, 0, -1])
def test_click_count_out_of_range_rejected(bad):
    with pytest.raises(StratValidationError) as ei:
        S.parse(_click_doc(bad), check_frames=False)
    assert any("clicks" in p for p in ei.value.problems)


def test_click_count_in_range_ok():
    st = S.parse(_click_doc(2), check_frames=False)
    assert [e for e in st.events if e.type == "click"][0].clicks == 2


# --- #F sync stability_frames must be >= 1 ---
def test_stability_frames_negative_rejected():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "a.png",
                       "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "stability_frames": -1}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("stability_frames" in p for p in ei.value.problems)


# --- #G per-event jitter_ms is honored and propagates through macro expansion ---
class _RecRng:
    def __init__(self):
        self.calls = []

    def uniform(self, a, b):
        self.calls.append((a, b))
        return 0.0


def test_per_event_jitter_overrides_config():
    st = S.StratFile(base_dir=".", events=[
        S.ClickEvent(1, 0, "click", "", 100, "left", Point(0.5, 0.5), 1, 0)])
    p, _, _, _ = build_player(st, cfg=mock_config(jitter_ms=0), clock=FakeClock())
    p._rng = _RecRng()
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert (-100, 100) in p._rng.calls   # used the event's jitter even though config jitter_ms=0


def test_expand_all_propagates_macro_jitter():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "place_tower", "tower": "x",
                       "hotbar_slot": 1, "pos": {"x": 0.5, "y": 0.5}, "jitter_ms": 30}]}
    prims = S.expand_all(S.parse(doc, check_frames=False).events)
    assert prims and all(p.jitter_ms == 30 for p in prims)


# --- #H session_max_minutes actually stops the loop ---
def test_session_cap_stops_loop():
    st = S.StratFile(base_dir=".", events=[])
    p, _, _, _ = build_player(
        st, cfg=mock_config(loop_count=0, session_max_minutes=1, min_inter_event_ms=60001),
        clock=FakeClock(), window=MockWindowProvider(frontmost=True))
    stats = p.run()
    assert stats.stopped_reason == "session cap reached"


# --- #I the GUI validates config (URL gate) before launching ---
def test_gui_play_rejects_invalid_private_server():
    ctrl, _, events = _setup(consent=True)
    assert ctrl.start_play("x.json", private_server="https://evil.com", accept_ban_risk=True) is False
    assert any(k == "error" and "invalid config" in str(p) for k, p in events)


# --- #J FOCUS_LOST settles (async activate) before STOP, uses the budget ---
class _FlakyWin(MockWindowProvider):
    def __init__(self):
        super().__init__(frontmost=False)
        self._n = 0

    def is_frontmost(self):
        self._n += 1
        return self._n >= 2   # not frontmost on the first check, then focus lands

    def activate(self):
        pass


def _rc(win, cfg=None):
    return RecoveryController(S.StratFile(base_dir="."), win, MockInputBackend(),
                             MockCaptureBackend(), MockComparator(), FakeClock(), cfg or mock_config())


def test_focus_lost_settles_then_resumes():
    assert _rc(_FlakyWin()).handle(FailureMode.FOCUS_LOST) == Outcome.RESUME


class _StuckWin(MockWindowProvider):
    def __init__(self):
        super().__init__(frontmost=False)

    def activate(self):
        pass  # activation never lands -> stays not-frontmost (vs MockWindowProvider which sets it True)


def test_focus_lost_stops_if_never_regained():
    assert _rc(_StuckWin()).handle(FailureMode.FOCUS_LOST) == Outcome.STOP


# --- #L window_title_match can't inject AppleScript ---
def test_validate_rejects_unsafe_window_title():
    assert any("window_title_match" in p for p in Config(window_title_match='x" to activate').validate())
    assert not any("window_title_match" in p for p in Config(window_title_match="Roblox Player").validate())


def test_activate_escapes_window_title(monkeypatch):
    import subprocess

    from tds_macro.window import QuartzWindowProvider
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (calls.append(a[0]), type("P", (), {"returncode": 1})())[1])
    QuartzWindowProvider(mock_config(window_title_match='evil" x')).activate()
    prog = calls[0][2]   # ["osascript", "-e", prog]
    assert '"evil\\" x"' in prog   # the injected quote is escaped, kept inside the string literal


# --- #M a shared modifier isn't released while another key still holds it ---
def test_release_key_refcounts_shared_modifier():
    with F.installed(F.make_pynput()):
        from tds_macro.input_backend import PynputInputBackend, key_to_pynput
        b = PynputInputBackend()
        b._ensure()
        shift = key_to_pynput("shift")
        b.press_key("a", modifiers=["shift"])
        b.press_key("c", modifiers=["shift"])
        b.release_key("a", modifiers=["shift"])
        assert b._kb.released.count(shift) == 0   # still held by 'c'
        b.release_key("c", modifiers=["shift"])
        assert b._kb.released.count(shift) == 1   # released once, by the last holder


# --- #N a kill-switch path that is a directory must not instant-panic ---
def test_killswitch_directory_does_not_panic(tmp_path):
    d = tmp_path / "ksdir"
    d.mkdir()
    hk = HotkeyManager(mock_config(killswitch_file=str(d)))
    hk.start()
    try:
        time.sleep(0.3)   # > one 0.2s poll
        assert not hk.events.panic.is_set()
    finally:
        hk.stop()


# --- #O a tiny clamped grab is inconclusive, real full-frame denial still caught ---
def test_screen_recording_tiny_frame_inconclusive(monkeypatch):
    import numpy as np

    from tds_macro import permissions
    from tds_macro.frame import Frame
    monkeypatch.setattr(permissions, "is_macos", lambda: True)

    class Cap:
        def __init__(self, shape):
            self.shape = shape

        def grab_window(self, geo):
            return Frame.from_numpy(np.zeros(self.shape, dtype="uint8"))

    assert permissions.check_screen_recording(Cap((1, 1, 4)), geo=object()) is True   # too small -> inconclusive
    assert permissions.check_screen_recording(Cap((100, 100, 4)), geo=object()) is False  # real black denial


# --- #P a mouse button held at recording stop is paired (not silently dropped) ---
def test_recorder_build_pairs_held_button(tmp_path):
    rec = Recorder(MockWindowProvider(frontmost=True, rect=(0, 0, 1600, 900)), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), hotkeys=None, clock=FakeClock())
    rec._refresh_geo()
    rec._on_click(800, 450, "left", True)   # press, still held when recording stops
    assert "left" in rec.coalescer._down
    st = rec.build(str(tmp_path / "s.json"))
    assert any(e.type == "click" for e in st.events)   # drained into a click, not dropped
    assert not rec.coalescer._down


# --- #R pause toggle is atomic ---
def test_toggle_pause_atomic_flips():
    ev = HotkeyEvents()
    assert ev.toggle_pause() is True and ev.pause.is_set()
    assert ev.toggle_pause() is False and not ev.pause.is_set()


# --- #S a record save failure falls back instead of losing the recording ---
def test_gui_record_save_failure_falls_back():
    saved = []

    def save(strat, p):
        if p == "out.json":
            raise OSError("read-only")
        saved.append(p)

    ctrl, h, events = _setup(save_strat=save)
    assert ctrl.start_record("out.json") is True
    assert h["recorder"].started.wait(1.0)
    ctrl.stop()
    assert saved and saved[0].endswith(".strat.json")   # saved to the fallback path
    assert any(k == "error" and "saved a copy to" in str(p) for k, p in events)
