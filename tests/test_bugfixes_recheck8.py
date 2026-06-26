"""Regression tests for the 8th workflow recheck (2 findings; see docs/BUGLOG.md)."""

import subprocess
import types

from tds_macro.config import Config, WindowBackendKind
from tds_macro.window import QuartzWindowProvider, make_window_provider


# #w8.1 activate() tries every candidate and only stops on an osascript that succeeded
def test_activate_falls_through_to_next_candidate(monkeypatch):
    tried = []

    def fake_run(cmd, **kw):
        name = cmd[2]  # the AppleScript string
        tried.append("RobloxPlayer" if "RobloxPlayer" in name else "Roblox")
        rc = 0 if "RobloxPlayer" in name else 1   # first app "fails", second succeeds
        return types.SimpleNamespace(returncode=rc)

    monkeypatch.setattr(subprocess, "run", fake_run)
    QuartzWindowProvider(Config(window_backend=WindowBackendKind.QUARTZ,
                                window_title_match="Roblox")).activate()
    assert tried == ["Roblox", "RobloxPlayer"]  # didn't stop after the first (rc=1)


# #w8.2 QUARTZ + rect override (no explicit retina) defaults retina to 2.0, not 1.0
def test_rect_override_retina_default():
    quartz = make_window_provider(Config(window_backend=WindowBackendKind.QUARTZ,
                                         window_rect_override=(0, 0, 800, 600)))
    assert quartz.get_geometry().retina == 2.0
    mock = make_window_provider(Config(window_backend=WindowBackendKind.MOCK,
                                       window_rect_override=(0, 0, 800, 600)))
    assert mock.get_geometry().retina == 1.0  # explicit mock stays internally consistent at 1.0
