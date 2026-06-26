"""Regression tests for review round 23-concluding batch: the fixes I applied before stopping the
loop — my own regressions (#4/#5 _RunComplete, #16 gui config) + clean wins (#2 modifiers, #8 URL
backslash) + the durable permission fix (#13). The remaining tail (#1,#3,#6,#9,#10,#11,#12,#14,#15)
was deferred — see docs/BUGLOG.md."""

import pytest

from tds_macro import strat as S
from tds_macro.config import Config, looks_like_roblox_url
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.window import MockWindowProvider
from tds_macro.recovery import MockRecoveryController, FailureMode
from tds_macro.gui import _build_config_from
from tds_macro.errors import StratValidationError

from helpers import build_player, mock_config, mk_sync


# --- #2 non-string modifier elements are rejected ---
def test_modifiers_non_string_rejected():
    doc = {"events": [{"id": 1, "t_ms": 0, "type": "key_press", "key": "a", "modifiers": [123]}]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("modifiers" in p for p in ei.value.problems)


# --- #4 _RunComplete is gated on IN_MATCH: a stale end-screen during LOBBY/join doesn't credit a run ---
def test_lobby_sync_victory_does_not_credit_run():
    rec = MockRecoveryController(classify_fn=lambda f: FailureMode.VICTORY)
    st = S.StratFile(base_dir=".", events=[],
                     join_sequence=[mk_sync(1, 0, "lobby", timeout=50, on_timeout="recover")])
    p, _, _, _ = build_player(st, cfg=mock_config(loop_count=5, max_consecutive_restarts=2,
                                                  recovery_check_every_ms=10),
                              capture=MockCaptureBackend(current_label="nomatch"),
                              clock=FakeClock(), recovery=rec, window=MockWindowProvider(frontmost=True))
    stats = p.run()
    assert stats.wins == 0 and stats.runs == 0   # a lobby-phase end screen rejoins, not a phantom win


# --- #5 the _RunComplete path releases held input (abandoned mid-sequence sync) ---
def test_run_complete_releases_input():
    rec = MockRecoveryController(classify_fn=lambda f: FailureMode.VICTORY)
    st = S.StratFile(base_dir=".", events=[mk_sync(1, 0, "wave1", timeout=50, on_timeout="recover")])
    p, inp, _, _ = build_player(st, cfg=mock_config(loop_count=1, recovery_check_every_ms=10),
                                capture=MockCaptureBackend(current_label="nomatch"),
                                clock=FakeClock(), recovery=rec)
    calls = [0]
    orig = inp.release_all
    inp.release_all = lambda: (calls.__setitem__(0, calls[0] + 1), orig())[1]
    stats = p.run()
    assert stats.wins == 1 and calls[0] >= 1   # win credited AND input released on the _RunComplete path


# --- #8 a backslash URL (host disagreement vs the browser) is rejected ---
@pytest.mark.parametrize("bad", [
    "https://evil.com\\@roblox.com/",
    "https://roblox.com\\.evil.com",
])
def test_url_backslash_rejected(bad):
    assert looks_like_roblox_url(bad) is False


# --- #16 run_gui's config builder honors the CLI-built base config ---
def test_build_config_from_honors_base():
    base = Config(private_server_url="roblox://base", loop_count=7)
    bc = _build_config_from(base)
    cfg = bc()                       # no overrides -> a copy of base
    assert cfg.private_server_url == "roblox://base" and cfg.loop_count == 7
    assert bc(loop_count=3).loop_count == 3            # explicit arg overrides
    assert bc(private_server="roblox://x").private_server_url == "roblox://x"
    # must not mutate the base across calls
    assert base.loop_count == 7 and base.private_server_url == "roblox://base"
