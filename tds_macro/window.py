"""Locate the Roblox window and report its content-box geometry + Retina scale.

The real provider uses Quartz (CGWindowListCopyWindowInfo); it matches on
``kCGWindowOwnerName`` (always present) not the title (only present with Screen
Recording permission), picks the largest layer-0 window, and re-queries every
loop so coordinates survive the user moving/resizing the window (plan S8, R05).
Platform imports are lazy so this module imports cleanly on Linux (M11).
"""

from __future__ import annotations

from typing import Protocol

from .config import Config, WindowBackendKind
from .errors import WindowNotFoundError
from .geometry import WindowGeometry


class WindowProvider(Protocol):
    def get_geometry(self) -> WindowGeometry: ...
    def is_frontmost(self) -> bool: ...
    def activate(self) -> None: ...


class MockWindowProvider:
    """Test/Linux provider driven by an explicit rect + scale."""

    def __init__(
        self,
        rect: tuple[int, int, int, int] = (0, 0, 1600, 900),
        retina: float = 1.0,
        monitor: tuple[int, int] = (0, 0),
        frontmost: bool = True,
    ) -> None:
        self.rect = rect
        self.retina = retina
        self.monitor = monitor
        self.frontmost = frontmost
        self.activate_calls = 0

    def get_geometry(self) -> WindowGeometry:
        x, y, w, h = self.rect
        return WindowGeometry(x, y, w, h, self.retina, self.monitor[0], self.monitor[1])

    def is_frontmost(self) -> bool:
        return self.frontmost

    def activate(self) -> None:
        self.activate_calls += 1
        self.frontmost = True


class QuartzWindowProvider:
    """macOS provider via CoreGraphics. Imports Quartz lazily."""

    OWNER_NAMES = ("Roblox", "RobloxPlayer")

    def __init__(self, config: Config) -> None:
        self.config = config
        self._owner_names = (
            (config.window_title_match,) if config.window_title_match else self.OWNER_NAMES
        )

    def _quartz(self):
        import Quartz  # type: ignore

        return Quartz

    def _find_window(self) -> dict:
        Q = self._quartz()
        wins = Q.CGWindowListCopyWindowInfo(
            Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
            Q.kCGNullWindowID,
        )
        best = None
        best_area = -1
        wanted = tuple(n.lower() for n in (*self._owner_names, *self.OWNER_NAMES))
        for w in wins or []:
            owner = (w.get("kCGWindowOwnerName") or "")
            if owner.lower() not in wanted:
                continue
            if int(w.get("kCGWindowLayer", 0)) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            area = float(b.get("Width", 0)) * float(b.get("Height", 0))
            if area > best_area:
                best_area = area
                best = w
        if best is None:
            raise WindowNotFoundError(
                f"No on-screen window owned by {self._owner_names} found. Is Roblox running?"
            )
        return best

    def _retina_for_point(self, x: float, y: float) -> float:
        if self.config.retina_scale_override is not None:
            return float(self.config.retina_scale_override)
        try:
            Q = self._quartz()
            max_displays = 16
            err, ids, count = Q.CGGetDisplaysWithPoint((x, y), max_displays, None, None)
            if not err and count:
                did = ids[0]
                bounds = Q.CGDisplayBounds(did)
                logical_w = bounds.size.width
                pixels_w = Q.CGDisplayPixelsWide(did)
                if logical_w:
                    return round(pixels_w / logical_w, 4)
        except Exception:
            pass
        return 2.0  # safe default on modern Macs; warns elsewhere if wrong

    def _monitor_origin_for_point(self, x: float, y: float) -> tuple[int, int]:
        try:
            Q = self._quartz()
            err, ids, count = Q.CGGetDisplaysWithPoint((x, y), 16, None, None)
            if not err and count:
                b = Q.CGDisplayBounds(ids[0])
                return (int(b.origin.x), int(b.origin.y))
        except Exception:
            pass
        return (0, 0)

    def get_geometry(self) -> WindowGeometry:
        w = self._find_window()
        b = w["kCGWindowBounds"]
        x, y = int(b["X"]), int(b["Y"])
        ww, wh = int(b["Width"]), int(b["Height"])
        retina = self._retina_for_point(x + ww / 2, y + wh / 2)
        mx, my = self._monitor_origin_for_point(x + ww / 2, y + wh / 2)
        return WindowGeometry(x, y, ww, wh, retina, mx, my)

    def is_frontmost(self) -> bool:
        try:
            from AppKit import NSWorkspace  # type: ignore

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            name = (app.localizedName() or "") if app else ""
            return name.lower() in (n.lower() for n in (*self._owner_names, *self.OWNER_NAMES))
        except Exception:
            return True  # best-effort; don't block if we can't tell

    def activate(self) -> None:
        import subprocess

        for name in self._owner_names:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{name}" to activate'],
                    check=False,
                    capture_output=True,
                    timeout=3,
                )
                return
            except Exception:
                continue


def make_window_provider(config: Config) -> WindowProvider:
    if config.window_backend == WindowBackendKind.MOCK or config.window_rect_override is not None:
        return MockWindowProvider(
            rect=config.window_rect_override or (0, 0, 1600, 900),
            retina=config.retina_scale_override or 1.0,
        )
    return QuartzWindowProvider(config)
