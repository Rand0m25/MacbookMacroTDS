"""Regression tests for review round 5 (see docs/BUGLOG.md)."""

import threading

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.recorder import EventCoalescer
from tds_macro.geometry import Point

import macfakes as F


# 1/2) schema_version NaN/Infinity reported, not OverflowError
@pytest.mark.parametrize("val", [float("inf"), float("nan"), float("-inf")])
def test_schema_version_non_finite_reported(val):
    with pytest.raises(StratValidationError) as ei:
        S.parse({"schema_version": val, "events": []}, check_frames=False)
    assert any("schema_version" in p and "finite" in p for p in ei.value.problems)


# 3) input codec: vk-only keys round-trip losslessly
def test_codec_vk_roundtrip():
    from tds_macro.input_backend import key_to_pynput, pynput_to_name

    class VkKey:
        char = None
        vk = 65

        def __str__(self):
            return "<65>"

    assert pynput_to_name(VkKey()) == "vk:65"  # not the truncated "<65>" -> "<"
    with F.installed(F.make_pynput()):
        assert key_to_pynput("vk:65") == ("vk", 65)  # reconstructed via from_vk, not from_char


# 4) permissions: window-not-found must NOT report Screen Recording as granted
def test_permissions_window_missing_not_granted(monkeypatch):
    from tds_macro import permissions
    from tds_macro.capture import MockCaptureBackend

    class _RaisingWindow:
        def get_geometry(self):
            raise RuntimeError("no window")

    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    with F.installed({"ApplicationServices": F.make_appservices(trusted=True)}):
        status = permissions.check_all(object(), capture=MockCaptureBackend(), window=_RaisingWindow())
    assert status.screen_recording is False
    assert any("window was not found" in m for m in status.messages)


# 5) EventCoalescer is thread-safe under concurrent listeners
def test_coalescer_concurrent_access():
    c = EventCoalescer(dead_zone=0.01)
    N = 500

    def keys():
        for i in range(N):
            c.on_key("a", True, i)
            c.on_key("a", False, i)

    def moves():
        for i in range(N):
            c.on_move(Point(0.5, 0.5), i)

    threads = [threading.Thread(target=keys) for _ in range(3)] + [threading.Thread(target=moves)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    evs = c.finish()
    # 3 key threads x N x (press+release) key events must all survive; the move
    # thread additionally contributes some flushed mouse_move anchors.
    assert sum(1 for e in evs if e.type == "key_press") == 3 * N
    assert sum(1 for e in evs if e.type == "key_release") == 3 * N
    assert len({e.id for e in evs}) == len(evs)  # ids unique -> _next_id not raced


# 6/7) CLI commands handle a missing/unreadable strat path gracefully
def test_cli_validate_missing_file_returns_1():
    from tds_macro.cli import build_parser
    args = build_parser().parse_args(["validate", "/no/such/strat.json"])
    assert args.func(args) == 1  # not an uncaught FileNotFoundError


def test_cli_calibrate_missing_file_returns_1():
    from tds_macro.cli import build_parser
    args = build_parser().parse_args(["calibrate", "/no/such/strat.json", "--mock"])
    assert args.func(args) == 1
