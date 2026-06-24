"""Shared test helpers: mock-backed Player builder + small constructors."""

from __future__ import annotations

from tds_macro.config import Config, InputBackendKind, ScreenBackendKind, WindowBackendKind
from tds_macro.clock import FakeClock
from tds_macro.window import MockWindowProvider
from tds_macro.capture import MockCaptureBackend
from tds_macro.input_backend import MockInputBackend
from tds_macro.visual import MockComparator
from tds_macro.recovery import MockRecoveryController
from tds_macro.frame import Frame
from tds_macro.engine import Player
from tds_macro.geometry import Rect
from tds_macro import strat as S


def mock_config(**kw) -> Config:
    c = Config(
        input_backend=InputBackendKind.MOCK, screen_backend=ScreenBackendKind.MOCK,
        window_backend=WindowBackendKind.MOCK, window_rect_override=(0, 0, 1600, 900),
        sync_park_cursor=False, recovery_check_every_ms=10 ** 9,  # guards off unless asked
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def ref_loader(path):
    """Engine sets .label after loading, so the value here is irrelevant."""
    return Frame.labelled("")


def mk_sync(eid, t, label, *, timeout=8000, on_timeout="abort", stability_frames=1,
            threshold=0.9, region=None, require_settled=False):
    return S.SyncPointEvent(eid, t, "sync_point", label=label, ref_frame=f"{label}.png",
                            region=region or Rect(0, 0, 1, 1), threshold=threshold,
                            timeout_ms=timeout, on_timeout=on_timeout,
                            stability_frames=stability_frames, require_settled=require_settled)


def build_player(strat, *, cfg=None, capture=None, clock=None, recovery=None,
                 hotkeys=None, input_backend=None, comparator=None, window=None):
    cfg = cfg or mock_config()
    window = window or MockWindowProvider(rect=(0, 0, 1600, 900))
    input_backend = input_backend or MockInputBackend()
    capture = capture or MockCaptureBackend(current_label="x")
    comparator = comparator or MockComparator()
    clock = clock or FakeClock()
    recovery = recovery or MockRecoveryController()
    player = Player(strat, window, input_backend, capture, comparator, clock, recovery, cfg,
                    hotkeys=hotkeys, ref_loader=ref_loader)
    return player, input_backend, capture, clock
