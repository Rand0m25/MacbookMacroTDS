"""Fast screen capture behind a mockable interface.

The real backend uses ``mss`` and is thread-confined (one mss instance per
thread via threading.local, plan S4) because mss is not thread-safe. It grabs
the window (or a sub-region for speed, R23) directly in the virtual-screen
coordinate space and returns a physical-pixel :class:`Frame`. Platform imports
are lazy (M11).
"""

from __future__ import annotations

import threading
from typing import Callable, Optional, Protocol

from .config import Config, ScreenBackendKind
from .frame import Frame
from .geometry import Rect, WindowGeometry


def _clamp_rect_to_bounds(left: int, top: int, width: int, height: int,
                          bounds: dict) -> tuple[int, int, int, int]:
    """Intersect a pixel rect with the virtual-screen ``bounds`` (an mss monitor dict
    ``{left,top,width,height}``), returning an in-bounds rect at least 1x1. A window dragged
    (partly) off-screen would otherwise make ``mss.grab`` raise ScreenShotError and end the
    whole run; clamping grabs the in-bounds sliver instead, which the comparator (it resizes
    live to the reference dims) scores low, so the normal sync-timeout/recovery path takes
    over rather than the run aborting (round 18 #1)."""
    bl, bt = int(bounds["left"]), int(bounds["top"])
    br, bb = bl + int(bounds["width"]), bt + int(bounds["height"])
    nl = max(bl, min(left, br - 1))
    nt = max(bt, min(top, bb - 1))
    nr = max(nl + 1, min(left + width, br))
    nb = max(nt + 1, min(top + height, bb))
    return nl, nt, nr - nl, nb - nt


class CaptureBackend(Protocol):
    def grab_window(self, geo: WindowGeometry) -> Frame: ...
    def grab_region(self, geo: WindowGeometry, region: Rect) -> Frame: ...
    def close(self) -> None: ...


class MockCaptureBackend:
    """Returns scripted frames.

    ``frame_fn(geo, region)`` lets a test decide what's "on screen" right now;
    by default it returns a frame carrying ``current_label`` so a label-matching
    MockComparator can drive scene logic deterministically.
    """

    def __init__(
        self,
        frame_fn: Optional[Callable[[WindowGeometry, Optional[Rect]], Frame]] = None,
        current_label: str = "unknown",
    ) -> None:
        self.frame_fn = frame_fn
        self.current_label = current_label
        self.grab_count = 0

    def _make(self, geo, region) -> Frame:
        self.grab_count += 1
        if self.frame_fn is not None:
            return self.frame_fn(geo, region)
        return Frame.labelled(self.current_label)

    def grab_window(self, geo: WindowGeometry) -> Frame:
        return self._make(geo, None)

    def grab_region(self, geo: WindowGeometry, region: Rect) -> Frame:
        return self._make(geo, region)

    def close(self) -> None:
        pass


class MssCaptureBackend:
    """macOS/Linux-X11 capture via mss; one mss instance per thread (S4)."""

    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss  # type: ignore

            # mss>=10 deprecates the lowercase mss.mss() factory in favour of
            # mss.MSS; prefer the new name and fall back for older versions.
            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            self._local.sct = sct
        return sct

    def _grab(self, left: int, top: int, width: int, height: int) -> Frame:
        # NOTE: coords are LOGICAL points and NO retina scaling is applied here, on purpose.
        # mss/darwin feeds this dict to CGWindowListCreateImage, whose CGRect is in global
        # display *points* (CGDisplayBounds is points too) and which returns a backing-
        # resolution (physical, 2x on Retina) image. So passing logical geo.x/geo.w is
        # correct and yields a physical-sized frame; multiplying by retina would capture a
        # 2x-too-large rect. (Reviewers repeatedly flag this as a bug — it is intended.)
        import numpy as np  # type: ignore

        width = max(1, int(width))
        height = max(1, int(height))
        sct = self._sct()
        # Keep the rect inside the virtual screen so an off-screen window can't crash mss (#1).
        left, top, width, height = _clamp_rect_to_bounds(int(left), int(top), width, height,
                                                         sct.monitors[0])
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        arr = np.asarray(shot)  # (H, W, 4) BGRA, physical pixels
        return Frame.from_numpy(arr)

    def grab_window(self, geo: WindowGeometry) -> Frame:
        return self._grab(geo.x, geo.y, geo.w, geo.h)

    def grab_region(self, geo: WindowGeometry, region: Rect) -> Frame:
        # Grab only the sub-rect (faster); region is normalized within the window.
        left = geo.x + region.x * geo.w
        top = geo.y + region.y * geo.h
        width = region.w * geo.w
        height = region.h * geo.h
        return self._grab(round(left), round(top), round(width), round(height))

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._local.sct = None


def make_capture_backend(config: Config) -> CaptureBackend:
    if config.screen_backend == ScreenBackendKind.MOCK:
        return MockCaptureBackend()
    return MssCaptureBackend()
