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


def test_full_run_joins_via_link_each_iteration():
    ml = MockLauncher()
    st = _strat(header=S.Header(private_server_url=URL),
                events=[S.KeyPressEvent(1, 0, "key_press", key="a")],
                run_end=S.RunEnd(victory=S.DetectorSpec("victory.png", S.Rect(0, 0, 1, 1), 0.9),
                                 timeout_ms=5000))
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=2, recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="victory"),
                              launcher=ml, clock=FakeClock())
    stats = p.run()
    assert stats.runs == 2 and ml.opened == [URL, URL]  # rejoined the same server each loop


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
