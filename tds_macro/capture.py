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
from .geometry import Coordinates, Rect, WindowGeometry


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
        import numpy as np  # type: ignore

        width = max(1, int(width))
        height = max(1, int(height))
        shot = self._sct().grab(
            {"left": int(left), "top": int(top), "width": width, "height": height}
        )
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
