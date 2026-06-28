"""Tests for the private-server-link join feature (Feature A)."""

import pytest

from tds_macro import strat as S
from tds_macro.config import Config, WindowBackendKind, looks_like_roblox_url
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.window import MockWindowProvider
from tds_macro.input_backend import MockInputBackend
from tds_macro.visual import MockComparator
from tds_macro.launcher import MockLauncher, MacLauncher, make_launcher
from tds_macro.recovery import RecoveryController, MockRecoveryController
from tds_macro.engine import _RestartLoop
from tds_macro.errors import WindowNotFoundError
from tds_macro.geometry import WindowGeometry

from helpers import build_player, mock_config

URL = "roblox://placeId=123&linkCode=abc"
WEB = "https://www.roblox.com/games/123/TDS?privateServerLinkCode=xyz"


# --- URL validation ---------------------------------------------------------
@pytest.mark.parametrize("url,ok", [
    (URL, True), (WEB, True), ("https://ro.blox.com/abc", True),
    ("roblox-player:1+launchmode:play", True),
    ("https://evil.com/roblox", False), ("ftp://roblox.com/x", False),
    ("not a url", False), ("", False),
])
def test_looks_like_roblox_url(url, ok):
    assert looks_like_roblox_url(url) is ok


def test_config_validate_rejects_bad_url_and_timeout():
    assert any("private_server_url" in p for p in Config(private_server_url="nope").validate())
    assert any("join_timeout_ms" in p for p in Config(join_timeout_ms=0).validate())
    assert Config(private_server_url=WEB, join_timeout_ms=10000).validate() == []


# --- strat header -----------------------------------------------------------
def test_header_url_roundtrips():
    st = S.parse({"header": {"private_server_url": URL}, "events": []}, check_frames=False)
    assert st.header.private_server_url == URL
    assert st.to_dict()["header"]["private_server_url"] == URL


def test_header_bad_url_reported():
    with pytest.raises(S.StratValidationError) as ei:
        S.parse({"header": {"private_server_url": "ftp://x"}, "events": []}, check_frames=False)
    assert any("private_server_url" in p for p in ei.value.problems)


# --- launcher ---------------------------------------------------------------
def test_mock_launcher_records():
    ml = MockLauncher()
    assert ml.open_url(URL) is True
    assert ml.open_url("") is False
    assert ml.opened == [URL, ""]


def test_make_launcher_picks_mock_for_mock_backend():
    assert isinstance(make_launcher(mock_config()), MockLauncher)
    assert isinstance(make_launcher(Config(window_backend=WindowBackendKind.QUARTZ)), MacLauncher)


def test_mac_launcher_dry_run_is_noop():
    assert MacLauncher(dry_run=True).open_url(URL) is False  # no subprocess in dry-run


# --- engine _join -----------------------------------------------------------
def _strat(**kw):
    return S.StratFile(base_dir=".", **kw)


def test_join_opens_link_and_awaits_map():
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL),
                expected_map_check=S.DetectorSpec("map.png", S.Rect(0, 0, 1, 1), 0.9))
    p, _, _, _ = build_player(st, cfg=mock_config(join_timeout_ms=5000),
                              capture=MockCaptureBackend(current_label="expected_map"),
                              launcher=ml, clock=FakeClock())
    p._join()
    assert ml.opened == [URL]  # opened the link; map matched -> proceeded


def test_join_timeout_routes_recovery():
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL),
                expected_map_check=S.DetectorSpec("map.png", S.Rect(0, 0, 1, 1), 0.9))
    rec = MockRecoveryController()  # default handle -> REJOIN -> _RestartLoop
    p, _, _, _ = build_player(st, cfg=mock_config(join_timeout_ms=100, recovery_check_every_ms=50),
                              capture=MockCaptureBackend(current_label="never"),
                              launcher=ml, recovery=rec, clock=FakeClock())
    with pytest.raises(_RestartLoop):
        p._join()
    assert ml.opened == [URL]


def test_join_without_link_plays_sequence_only():
    ml = MockLauncher()
    st = _strat(join_sequence=[S.KeyPressEvent(1, 0, "key_press", key="space")])
    p, inp, _, _ = build_player(st, cfg=mock_config(), launcher=ml, clock=FakeClock())
    p._join()
    assert ml.opened == []  # no link -> launcher untouched
    assert any(e.get("action") == "key_press" and e.get("key") == "space"
               for e in inp.events)  # join_sequence still played


def test_full_run_opens_private_server_only_once():
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL),
                events=[S.KeyPressEvent(1, 0, "key_press", key="a")],
                run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9),
                                 timeout_ms=5000))
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=2, recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="victory"),
                              launcher=ml, clock=FakeClock())
    stats = p.run()
    # two matches farmed, but the private-server link is opened ONCE — you stay in the same server
    # across matches; recovery (not the main loop) re-opens it only on a real disconnect.
    assert stats.runs == 2 and ml.opened == [URL]


def test_join_sequence_replays_every_loop_without_reopening_link():
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL),
                join_sequence=[S.KeyPressEvent(1, 0, "key_press", key="space")],
                events=[S.KeyPressEvent(2, 0, "key_press", key="a")],
                run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9),
                                 timeout_ms=5000))
    p, inp, _, _ = build_player(st, cfg=mock_config(loop_count=2, recovery_check_every_ms=10),
                                capture=MockCaptureBackend(current_label="victory"),
                                launcher=ml, clock=FakeClock())
    stats = p.run()
    # link opened once, but the lobby re-queue clicks (join_sequence) still fire each of the 2 loops
    assert stats.runs == 2 and ml.opened == [URL]
    queued = sum(1 for e in inp.events if e.get("action") == "key_press" and e.get("key") == "space")
    assert queued == 2


def test_join_reopens_after_explicit_reset_flag():
    # the once-per-session guard is just a flag; clearing it (as a future re-join hook could) re-opens
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL))
    p, _, _, _ = build_player(st, cfg=mock_config(join_timeout_ms=5000), launcher=ml, clock=FakeClock())
    p._join()
    p._private_server_opened = False
    p._join()
    assert ml.opened == [URL, URL]


# --- cold start: launch Roblox via the private-server link when no window is detected ----------
class _LaunchableWindow:
    """get_geometry() raises WindowNotFoundError until `appear_after` calls, then returns a window —
    simulating Roblox not running, then its window appearing after the link launches it."""

    def __init__(self, appear_after=1, rect=(0, 0, 1600, 900)):
        self.appear_after = appear_after
        self.rect = rect
        self.calls = 0
        self.frontmost = True
        self.activate_calls = 0

    def get_geometry(self):
        self.calls += 1
        if self.calls <= self.appear_after:
            raise WindowNotFoundError("Roblox not running yet")
        x, y, w, h = self.rect
        return WindowGeometry(x, y, w, h, 1.0, 0, 0)

    def is_frontmost(self):
        return self.frontmost

    def activate(self):
        self.activate_calls += 1
        self.frontmost = True


def test_ensure_window_noop_when_window_present():
    ml = MockLauncher()
    p, _, _, _ = build_player(_strat(header=S.Header(private_server_url=URL)),
                              window=MockWindowProvider(), launcher=ml, clock=FakeClock())
    p._ensure_window_or_launch()
    assert ml.opened == []  # window already up -> nothing launched


def test_ensure_window_launches_and_acquires_after_appearing():
    ml = MockLauncher()
    win = _LaunchableWindow(appear_after=2)  # construction(1) defers; then ensure polls until it appears
    p, _, _, _ = build_player(_strat(header=S.Header(private_server_url=URL)), window=win, launcher=ml,
                              cfg=mock_config(launch_timeout_ms=10000, recovery_check_every_ms=10),
                              clock=FakeClock())
    assert p._geo is None  # deferred at construction: Roblox wasn't running but a link is set
    p._ensure_window_or_launch()
    assert ml.opened == [URL] and p._private_server_opened is True
    assert p._geo is not None and win.calls >= 3  # retried get_geometry until the window appeared


def test_full_run_launches_via_link_from_cold_start():
    ml = MockLauncher()
    win = _LaunchableWindow(appear_after=1)
    st = _strat(header=S.Header(private_server_url=URL),
                events=[S.KeyPressEvent(1, 0, "key_press", key="a")],
                run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9),
                                 timeout_ms=5000))
    p, _, _, _ = build_player(st, window=win, launcher=ml,
                              cfg=mock_config(loop_count=1, launch_timeout_ms=10000, recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="victory"), clock=FakeClock())
    stats = p.run()
    # launched once via the link (the cold-start open), NOT re-opened by _join afterwards
    assert ml.opened == [URL] and stats.runs == 1


def test_launch_times_out_when_window_never_appears():
    ml = MockLauncher()
    win = _LaunchableWindow(appear_after=10_000)  # never comes up within the budget
    p, _, _, _ = build_player(_strat(header=S.Header(private_server_url=URL)), window=win, launcher=ml,
                              cfg=mock_config(launch_timeout_ms=100, recovery_check_every_ms=50),
                              clock=FakeClock())
    p._ensure_window_or_launch()  # opens link, waits, gives up without raising (loop's _arm surfaces it)
    assert ml.opened == [URL] and p._geo is None


def test_cold_start_timeout_yields_error_stop_with_zero_runs():
    # run()-level outcome of a launch that never produces a window: the WindowNotFoundError surfaces
    # as a graceful error stop with runs == 0 (this is exactly what cli._play_exit_code / the GUI map
    # to a "could not start" failure — review round 24 #1/#2).
    ml = MockLauncher()
    win = _LaunchableWindow(appear_after=10_000)
    st = _strat(header=S.Header(private_server_url=URL),
                events=[S.KeyPressEvent(1, 0, "key_press", key="a")])
    p, _, _, _ = build_player(st, window=win, launcher=ml,
                              cfg=mock_config(loop_count=1, launch_timeout_ms=100, recovery_check_every_ms=50),
                              clock=FakeClock())
    stats = p.run()
    assert ml.opened == [URL]  # it DID try to launch
    assert stats.runs == 0 and stats.stopped_reason.startswith("error:")  # but never got going


def test_non_window_error_at_construction_is_not_deferred():
    # a NON-WindowNotFoundError at construction must fail fast, NOT be swallowed into a needless launch
    # (review round 24 #3) — even when a private-server link is configured.
    class _BadWindow:
        def get_geometry(self):
            raise RuntimeError("quartz exploded")

        def is_frontmost(self):
            return True

        def activate(self):
            pass

    with pytest.raises(RuntimeError):
        build_player(_strat(header=S.Header(private_server_url=URL)), window=_BadWindow())


def test_launch_poll_propagates_non_window_error():
    # during the launch wait, only "window not up yet" (WindowNotFoundError) is retried; any other
    # error must surface immediately rather than being retried for launch_timeout_ms (review round 24 #3).
    class _W:
        def __init__(self):
            self.calls = 0

        def get_geometry(self):
            self.calls += 1
            if self.calls == 1:
                raise WindowNotFoundError("not running yet")  # defer at construction
            raise RuntimeError("quartz exploded mid-launch")   # a genuine error while polling

        def is_frontmost(self):
            return True

        def activate(self):
            pass

    ml = MockLauncher()
    p, _, _, _ = build_player(_strat(header=S.Header(private_server_url=URL)), window=_W(), launcher=ml,
                              cfg=mock_config(launch_timeout_ms=5000, recovery_check_every_ms=10),
                              clock=FakeClock())
    with pytest.raises(RuntimeError):
        p._ensure_window_or_launch()
    assert ml.opened == [URL]  # the launch was attempted, then the real error surfaced (not retried)


def test_missing_window_without_link_still_fails_fast():
    # no link -> nothing to launch -> preserve the original construction-time failure (CLI/GUI exit 1)
    with pytest.raises(WindowNotFoundError):
        build_player(_strat(), window=_LaunchableWindow(appear_after=10_000))


def test_dry_run_missing_window_fails_fast_not_deferred():
    # dry-run never opens links, so a missing window can't be launched -> fail fast at construction
    with pytest.raises(WindowNotFoundError):
        build_player(_strat(header=S.Header(private_server_url=URL)),
                     window=_LaunchableWindow(appear_after=10_000), cfg=mock_config(dry_run=True))


# --- recovery relaunch precedence -------------------------------------------
def _recovery(cfg, header_url=""):
    st = S.StratFile(header=S.Header(private_server_url=header_url), base_dir=".")
    ml = MockLauncher()
    rc = RecoveryController(st, MockWindowProvider(), MockInputBackend(), MockCaptureBackend(),
                            MockComparator(), FakeClock(), cfg, launcher=ml)
    return rc, ml


def test_relaunch_prefers_private_server_then_header_then_relaunch_url():
    # config.private_server_url wins
    rc, ml = _recovery(mock_config(private_server_url=URL, relaunch_url=WEB), header_url="roblox://hdr")
    rc._relaunch_experience()
    assert ml.opened == [URL]

    # else header.private_server_url
    rc, ml = _recovery(mock_config(relaunch_url=WEB), header_url="roblox://hdr")
    rc._relaunch_experience()
    assert ml.opened == ["roblox://hdr"]

    # else legacy relaunch_url
    rc, ml = _recovery(mock_config(relaunch_url=WEB))
    rc._relaunch_experience()
    assert ml.opened == [WEB]


# --- recorder stamping ------------------------------------------------------
def test_recorder_stamps_url_into_header():
    from tds_macro.recorder import Recorder
    from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
    cfg = mock_config(private_server_url=URL)
    rec = Recorder(MockWindowProvider(rect=(0, 0, 100, 100)), MockInputBackend(),
                   MockCaptureBackend(), cfg, HotkeyManager(cfg, HotkeyEvents()))
    st = rec.build("out.strat.json")
    assert st.header.private_server_url == URL
