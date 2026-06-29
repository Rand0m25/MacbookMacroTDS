"""Recorder coalescer rules (plan S5)."""

from tds_macro.recorder import EventCoalescer, Recorder
from tds_macro.geometry import Point
from tds_macro.window import MockWindowProvider
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
from tds_macro.clock import FakeClock

from helpers import mock_config


def test_control_hotkeys_excluded_from_recording():
    # The panic (F8) / mark-sync (F10) / pause (F7) / start (F9) hotkeys drive the recording SESSION;
    # capturing them as game input would re-press them on replay — and a recorded F8 would make the
    # macro stop ITSELF mid-run (seen in a real user recording). They must never enter the strat.
    rec = Recorder(MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()),
                   clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    for k in ["f8", "F10", "a", "f7", "f9"]:  # mixed case to prove case-insensitive filtering
        rec._on_press(k)
        rec._on_release(k)
    evs = rec.coalescer.finish()
    keys = [e.key for e in evs if e.type in ("key_press", "key_release")]
    assert keys == ["a", "a"]  # only the real game key survives (paired press + release)


def test_combo_hotkey_constituents_excluded_from_recording():
    # a combo hotkey (e.g. pause = ctrl+p) fires as individual keys; each constituent must be filtered,
    # or replaying them could reconstitute the combo and pause/stop the macro itself (review round 25).
    cfg = mock_config(pause_hotkey="ctrl+p")
    rec = Recorder(MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), cfg, HotkeyManager(cfg, HotkeyEvents()), clock=FakeClock())
    rec._t0 = 0.0
    rec._refresh_geo()
    for k in ["p", "a"]:  # 'p' is part of ctrl+p; 'a' is real game input
        rec._on_press(k)
        rec._on_release(k)
    keys = [e.key for e in rec.coalescer.finish() if e.type in ("key_press", "key_release")]
    assert keys == ["a", "a"]


def test_double_click_merge_and_pre_move_dropped():
    c = EventCoalescer(dead_zone=0.01, double_click_ms=250)
    c.on_move(Point(0.5, 0.5), 100)             # free move before click -> dropped
    c.on_button(Point(0.5, 0.5), "left", True, 150)
    c.on_button(Point(0.5, 0.5), "left", False, 170)
    c.on_button(Point(0.5, 0.5), "left", True, 200)
    c.on_button(Point(0.5, 0.5), "left", False, 210)
    evs = c.finish()
    assert [e.type for e in evs] == ["click"]
    assert evs[0].clicks == 2


def test_rapid_spam_clicks_not_collapsed_to_one():
    # 5 rapid clicks at one spot must NOT all merge into a single double-click; only one pair
    # coalesces, so all 5 clicks survive (e.g. [2, 2, 1]) — round 26 #9.
    c = EventCoalescer(dead_zone=0.01, double_click_ms=250)
    t = 0
    for _ in range(5):
        c.on_button(Point(0.5, 0.5), "left", True, t)
        c.on_button(Point(0.5, 0.5), "left", False, t + 10)
        t += 130  # < double_click_ms apart, same spot
    clicks = [e.clicks for e in c.finish() if e.type == "click"]
    assert sum(clicks) == 5  # all 5 preserved, not collapsed to one clicks=2


def test_two_far_clicks_not_merged():
    c = EventCoalescer(dead_zone=0.01, double_click_ms=250)
    c.on_button(Point(0.2, 0.2), "left", True, 0)
    c.on_button(Point(0.2, 0.2), "left", False, 10)
    c.on_button(Point(0.8, 0.8), "left", True, 50)
    c.on_button(Point(0.8, 0.8), "left", False, 60)
    evs = c.finish()
    assert [e.type for e in evs] == ["click", "click"]
    assert all(e.clicks == 1 for e in evs)


def test_drag_detected_by_deadzone():
    c = EventCoalescer(dead_zone=0.01)
    c.on_button(Point(0.2, 0.5), "left", True, 0)
    c.on_move(Point(0.6, 0.5), 50)
    c.on_button(Point(0.6, 0.5), "left", False, 100)
    evs = c.finish()
    assert evs[0].type == "drag"
    assert evs[0].frm.x == 0.2 and evs[0].to.x == 0.6
    assert evs[0].duration_ms == 100


def test_free_move_flushed_before_key():
    c = EventCoalescer(dead_zone=0.01)
    c.on_move(Point(0.3, 0.3), 10)
    c.on_key("e", True, 20)
    c.on_key("e", False, 25)
    assert [e.type for e in c.finish()] == ["mouse_move", "key_press", "key_release"]


def test_tiny_move_while_held_is_click_not_drag():
    c = EventCoalescer(dead_zone=0.02)
    c.on_button(Point(0.5, 0.5), "left", True, 0)
    c.on_move(Point(0.505, 0.5), 5)  # within dead zone
    c.on_button(Point(0.505, 0.5), "left", False, 10)
    evs = c.finish()
    assert [e.type for e in evs] == ["click"]


def test_events_have_monotonic_ids_and_sorted_time():
    c = EventCoalescer(dead_zone=0.01)
    c.on_key("a", True, 100)
    c.on_key("a", False, 110)
    c.on_button(Point(0.1, 0.1), "left", True, 50)
    c.on_button(Point(0.1, 0.1), "left", False, 60)
    evs = c.finish()
    assert [e.t_ms for e in evs] == sorted(e.t_ms for e in evs)
    assert len({e.id for e in evs}) == len(evs)


def test_capture_sync_point_honors_label_prefix(tmp_path, monkeypatch):
    # a join/leave recording sets a target-specific prefix so its frames are named join_sync_N / leave_sync_N
    import os
    import tds_macro.pngio as pngio
    written = []
    monkeypatch.setattr(pngio, "frame_to_rgba_bytes", lambda f: (b"\x00\x00\x00\x00", 1, 1))
    monkeypatch.setattr(pngio, "write_png", lambda path, *a, **k: written.append(os.path.abspath(path)))
    rec = Recorder(MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()),
                   clock=FakeClock())
    rec._strat_dir = str(tmp_path)
    rec._refresh_geo()
    rec.sync_label_prefix = "join_sync"
    sp = rec.capture_sync_point()
    assert sp.label == "join_sync_1"
    assert sp.ref_frame == os.path.join("frames", "join_sync_1.png")
    assert written and written[0].endswith(os.path.join("frames", "join_sync_1.png"))


def test_join_recording_frames_do_not_overwrite_main_timeline_frames(tmp_path, monkeypatch):
    # Repro of the cross-target collision: recording the main timeline writes frames/sync_1.png; a later
    # join recording into the SAME strat dir must write a DIFFERENT file, or it clobbers the main
    # timeline's reference image (its sync would then compare against the lobby forever).
    import os
    import tds_macro.pngio as pngio
    written = []
    monkeypatch.setattr(pngio, "frame_to_rgba_bytes", lambda f: (b"\x00\x00\x00\x00", 1, 1))
    monkeypatch.setattr(pngio, "write_png", lambda path, *a, **k: written.append(os.path.abspath(path)))

    def mk(prefix):
        r = Recorder(MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True), MockInputBackend(),
                     MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()),
                     clock=FakeClock())
        r._strat_dir = str(tmp_path)
        r._refresh_geo()
        r.sync_label_prefix = prefix
        return r

    sp_main = mk("sync").capture_sync_point()          # main timeline (default prefix)
    sp_join = mk("join_sync").capture_sync_point()      # join sequence into the same strat
    assert sp_main.ref_frame == os.path.join("frames", "sync_1.png")
    assert sp_join.ref_frame == os.path.join("frames", "join_sync_1.png")
    assert written[0] != written[1]  # distinct files -> the main timeline's frame is never clobbered


def test_capture_sync_point_defaults_on_timeout_continue(tmp_path, monkeypatch):
    # auto-marked syncs default to a FULL-window region that can't reliably re-match a live match, so
    # the default on_timeout must be "continue" (not "recover") -> a flaky sync can't trigger leave/restart.
    import tds_macro.pngio as pngio
    monkeypatch.setattr(pngio, "frame_to_rgba_bytes", lambda f: (b"\x00\x00\x00\x00", 1, 1))
    monkeypatch.setattr(pngio, "write_png", lambda *a, **k: None)
    rec = Recorder(MockWindowProvider(rect=(0, 0, 1000, 1000), frontmost=True), MockInputBackend(),
                   MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()),
                   clock=FakeClock())
    rec._strat_dir = str(tmp_path)
    rec._refresh_geo()
    sp = rec.capture_sync_point()
    assert sp.on_timeout == "continue"
