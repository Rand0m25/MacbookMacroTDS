"""Regression tests for review round 15 (3 findings; see docs/BUGLOG.md)."""

from tds_macro import strat as S
from tds_macro.clock import FakeClock
from tds_macro.capture import MockCaptureBackend
from tds_macro.geometry import WindowGeometry
from tds_macro.engine import WaitResult

from helpers import build_player, mock_config, mk_sync


# #w-settle-mask: the require_settled frame-to-frame check must honor the sync mask, or a
# masked-out dynamic region would keep "settled" False forever -> spurious TIMEOUT.
def test_require_settled_uses_mask_in_settle_check():
    # A comparator where the FULL-frame score is low (a region keeps changing) but the
    # MASKED score is high — mirrors a timer/animation masked out of the match.
    class MaskAwareComparator:
        def score(self, live, ref, method=None, mask=None):
            # without a mask: "unsettled" (0.0); with a mask: identical/settled+matched (1.0)
            return 1.0 if mask else 0.0

    st = S.StratFile(base_dir=".")
    sync = mk_sync(1, 0, "s", require_settled=True, stability_frames=1, timeout=5000,
                   region=None)
    sync.mask = [S.Rect(0.4, 0.0, 0.2, 0.1)]  # a mask is configured
    p, _, _, _ = build_player(st, cfg=mock_config(), comparator=MaskAwareComparator(),
                              capture=MockCaptureBackend(current_label="s"), clock=FakeClock())
    res, _ = p._adaptive_wait(sync)
    assert res == WaitResult.FIRE  # masked settle check succeeds (was TIMEOUT without the mask)


# #w-keynone: an unmappable multi-char key is skipped, not turned into a wrong character
def test_unmappable_key_is_skipped_not_truncated():
    import macfakes as F
    with F.installed(F.make_pynput()):
        from tds_macro.input_backend import key_to_pynput, PynputInputBackend
        assert key_to_pynput("weirdgrapheme") is None     # skip, don't return 'w'
        assert key_to_pynput("a") == "a"                  # normal keys still map
        pib = PynputInputBackend(); pib._ensure()
        pib.press_key("weirdgrapheme")                    # must not press anything / not track it
        assert "weirdgrapheme" not in pib._held_keys
        assert pib._kb.pressed == []                      # nothing injected
        pib.press_key("a")
        assert "a" in pib._held_keys and pib._kb.pressed == ["a"]


# #w-deadzone: a zero-size (minimized) window must not blow up the recorder dead-zone
def test_recorder_deadzone_guarded_on_zero_size_window():
    from tds_macro.recorder import Recorder
    from tds_macro.hotkeys import HotkeyManager, HotkeyEvents

    class ShrinkingWindow:
        def __init__(self): self.geo = WindowGeometry(0, 0, 1600, 900, 1.0, 0, 0)
        def get_geometry(self): return self.geo
        def is_frontmost(self): return True
        def activate(self): pass

    win = ShrinkingWindow()
    rec = Recorder(win, None, MockCaptureBackend(), mock_config(), HotkeyManager(mock_config(), HotkeyEvents()))
    rec._refresh_geo()
    good_dz = rec.coalescer.dead_zone
    assert 0 < good_dz <= 0.5
    win.geo = WindowGeometry(0, 0, 0, 0, 1.0, 0, 0)  # minimized: w=h=0
    rec._refresh_geo()
    assert rec.coalescer.dead_zone == good_dz  # unchanged, NOT 6.0
