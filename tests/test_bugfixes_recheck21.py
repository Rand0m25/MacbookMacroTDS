"""Regression tests for review round 22b (deep v5, second pass: 10 fixes).

Again several guard regressions/gaps from my own fixes: #6 the round-21 hotkeys ImportError ordering,
#7 the round-17 recorder drain race, #8 the cmd_record analog of the GUI validation gap."""

import logging
from types import SimpleNamespace

import pytest

import macfakes as F
from tds_macro import strat as S
from tds_macro import cli
from tds_macro.clock import FakeClock
from tds_macro.engine import RunState
from tds_macro.geometry import Point
from tds_macro.gui import GuiController
from tds_macro.hotkeys import HotkeyManager
from tds_macro.input_backend import _lerp_steps, _MAX_LERP_STEPS, _ease, PynputInputBackend
from tds_macro.errors import StratValidationError

from helpers import build_player, mock_config


# --- #1 run_end timeout_ms must be >= 0 ---
def test_run_end_negative_timeout_rejected():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [], "run_end": {"timeout_ms": -5000}}, check_frames=False)
    assert any("timeout_ms" in p for p in ei.value.problems)


# --- #2 the 'expect_' sync label prefix is reserved ---
def test_expect_label_prefix_reserved():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "a.png",
                       "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "label": "expect_boss"}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("expect_" in p for p in ei.value.problems)


# --- #3 an absurd huge time field is rejected (would hang sleep_until) ---
def test_huge_time_field_rejected():
    doc = {"events": [{"id": 1, "t_ms": 1e300, "type": "wait", "duration_ms": 0}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("t_ms" in p for p in ei.value.problems)


# --- #4 easing: validated, real curve, threaded through to the backend ---
def test_easing_validated():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "mouse_move", "pos": {"x": 0.5, "y": 0.5},
                       "duration_ms": 100, "easing": "bogus"}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("easing" in p for p in ei.value.problems)


def test_ease_curve_values():
    assert _ease("linear", 0.5) == 0.5            # identity -> existing strats unchanged
    assert _ease("ease_in", 0.5) == 0.25
    assert _ease("ease_out", 0.5) == 0.75
    assert _ease("ease_in_out", 0.5) == 0.5
    for name in ("linear", "ease_in", "ease_out", "ease_in_out"):
        assert _ease(name, 0.0) == 0.0 and _ease(name, 1.0) == 1.0   # endpoints pinned


def test_easing_reaches_backend():
    st = S.StratFile(base_dir=".", events=[
        S.MouseMoveEvent(1, 0, "mouse_move", "", 0, Point(0.5, 0.5), 100, "ease_in")])
    p, inp, _, _ = build_player(st, clock=FakeClock())
    p._play_sequence(st.events, RunState.IN_MATCH)
    assert any(e.get("action") == "move" and e.get("easing") == "ease_in" for e in inp.events)


# --- #5 _lerp_steps is capped (no mock busy-spin) ---
def test_lerp_steps_capped():
    assert _lerp_steps(1e9, 120) == _MAX_LERP_STEPS
    assert _lerp_steps(1000, 120) == 120   # normal: 1s * 120hz, unchanged


# --- #6 a build-time listener failure (incl ImportError) WARNS, not silent ---
def test_hotkey_build_importerror_is_logged(caplog):
    mods = F.make_pynput()

    def _boom(mapping):
        raise ImportError("deferred backend missing")   # surfaces during GlobalHotKeys construction

    mods["pynput.keyboard"].GlobalHotKeys = _boom
    hk = HotkeyManager(mock_config(killswitch_file=""))
    with F.installed(mods), caplog.at_level(logging.WARNING):
        hk.start()
    hk.stop()
    assert "DISABLED" in caplog.text   # not swallowed by the quiet ImportError clause


# --- #7 stop_listeners stops AND joins (quiesces in-flight callbacks before the drain) ---
def test_stop_listeners_joins():
    class L:
        def __init__(self):
            self.stopped = self.joined = False

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.joined = True

    b = PynputInputBackend()
    b._mouse_listener, b._kb_listener = L(), L()
    ml, kl = b._mouse_listener, b._kb_listener
    b.stop_listeners()
    assert ml.stopped and ml.joined and kl.stopped and kl.joined


# --- #8 cmd_record rejects a bad --private-server BEFORE recording ---
def test_cmd_record_rejects_bad_private_server():
    args = SimpleNamespace(private_server="notaurl", strat="x.strat.json", frames_dir=None)
    assert cli.cmd_record(args) == 1   # validated up front, never reaches the (blocking) recorder


# --- #9 a consent-write failure warns instead of silently re-prompting forever ---
def test_consent_write_failure_warns(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONSENT_PATH", "/no_such_dir_xyz/consent")
    assert cli._check_consent(SimpleNamespace(accept_ban_risk=True)) is True
    out = capsys.readouterr()
    assert "could not save" in (out.out + out.err)


# --- #10 a failed worker-thread start resets the controller to idle ---
def test_spawn_failure_resets_to_idle():
    ctrl = GuiController()

    class _BadThread:
        def start(self):
            raise RuntimeError("can't start new thread")

    class _HK:
        def start(self):
            pass

        def stop(self):
            pass

    with ctrl._lock:
        ctrl._activity, ctrl._thread = "play", _BadThread()
    assert ctrl._spawn(_HK(), "play") is False
    assert not ctrl.is_busy() and ctrl._activity == "idle"
