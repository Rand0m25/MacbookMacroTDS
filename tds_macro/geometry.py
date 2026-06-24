"""Resolution- and Retina-independent coordinate math.

Everything the macro stores is a normalized 0..1 fraction of the Roblox window's
content box. Only this module (and the capture layer) ever deals in absolute
logical points or physical pixels. See PLAN.md section 1 and section 8.1.

Conventions (all top-left origin, global logical points unless noted):
  Wx,Wy,Ww,Wh : Roblox window content-box origin + size, logical points.
  Mx,My       : top-left of the monitor that ``mss`` grabbed (may be negative
                on a multi-display setup that extends left/up).
  retina      : backing scale (physical pixels per logical point); 2.0 on Retina.

  A) norm -> logical  (for input injection):   px = Wx + nx*Ww
  B) logical -> norm  (recorder):              nx = (px - Wx)/Ww
  C) norm -> physical, MONITOR-RELATIVE:       px_phys = ((Wx-Mx) + nx*Ww)*retina
  D) physical -> norm (inverse of C):          nx = ((px_phys/retina) - (Wx-Mx))/Ww
  E) region crop in a window image (Iw,Ih):    [round(ry*Ih):round((ry+rh)*Ih),
                                                round(rx*Iw):round((rx+rw)*Iw)]

This module is pure stdlib; it has no third-party or OS imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    """A normalized point: x,y in 0..1 of the window content box."""

    x: float
    y: float

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, d: dict) -> "Point":
        return cls(float(d["x"]), float(d["y"]))


@dataclass(frozen=True)
class Rect:
    """A normalized rectangle: x,y top-left and w,h, all 0..1 of the window."""

    x: float
    y: float
    w: float
    h: float

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict) -> "Rect":
        return cls(float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"]))


@dataclass(frozen=True)
class WindowGeometry:
    """The Roblox window content box, in global logical points, plus Retina scale.

    ``monitor_x``/``monitor_y`` are the origin of the display the capture backend
    grabs from, so physical-pixel conversions are relative to that grab's image
    (plan M6) rather than to the global desktop origin.
    """

    x: int
    y: int
    w: int
    h: int
    retina: float = 1.0
    monitor_x: int = 0
    monitor_y: int = 0

    @property
    def aspect(self) -> float:
        return self.w / self.h if self.h else 0.0


def clamp01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


class Coordinates:
    """Binds a live :class:`WindowGeometry` to do the A/B/C/D/E conversions."""

    # How far outside [0,1] a recorded event may fall before we treat it as
    # "happened outside the Roblox window" and drop it (plan B / R27).
    OUT_OF_WINDOW_SLACK = 0.02

    def __init__(self, geo: WindowGeometry) -> None:
        self.geo = geo

    # --- A: normalized -> absolute logical points (for clicks) ---
    def norm_to_logical(self, p: Point) -> tuple[float, float]:
        g = self.geo
        return (g.x + p.x * g.w, g.y + p.y * g.h)

    # --- B: absolute logical points -> normalized (recorder) ---
    def logical_to_norm(self, px: float, py: float) -> Point:
        g = self.geo
        return Point((px - g.x) / g.w, (py - g.y) / g.h)

    # --- C: normalized -> physical pixels, relative to the grabbed monitor ---
    def norm_to_physical(self, p: Point) -> tuple[float, float]:
        g = self.geo
        px = ((g.x - g.monitor_x) + p.x * g.w) * g.retina
        py = ((g.y - g.monitor_y) + p.y * g.h) * g.retina
        return (px, py)

    # --- D: physical pixels -> normalized (inverse of C) ---
    def physical_to_norm(self, px_phys: float, py_phys: float) -> Point:
        g = self.geo
        nx = ((px_phys / g.retina) - (g.x - g.monitor_x)) / g.w
        ny = ((py_phys / g.retina) - (g.y - g.monitor_y)) / g.h
        return Point(nx, ny)

    # --- E: crop box for a normalized region inside a window image (Iw,Ih) ---
    @staticmethod
    def region_crop_box(region: Rect, img_w: int, img_h: int) -> tuple[int, int, int, int]:
        x0 = round(region.x * img_w)
        y0 = round(region.y * img_h)
        x1 = round((region.x + region.w) * img_w)
        y1 = round((region.y + region.h) * img_h)
        # Clamp the lower bounds to leave room for a >=1px box, so the box is
        # always non-empty AND in-bounds even for a region at the far edge.
        x0 = max(0, min(x0, max(0, img_w - 1)))
        y0 = max(0, min(y0, max(0, img_h - 1)))
        x1 = max(x0 + 1, min(x1, img_w))
        y1 = max(y0 + 1, min(y1, img_h))
        return (x0, y0, x1, y1)

    def is_out_of_window(self, p: Point) -> bool:
        s = self.OUT_OF_WINDOW_SLACK
        return not (-s <= p.x <= 1.0 + s and -s <= p.y <= 1.0 + s)


def aspect_mismatch(geo: WindowGeometry, header_aspect: float, tol: float) -> bool:
    """True if the live window aspect differs from the recorded one beyond tol."""
    if not header_aspect:
        return False
    return abs(geo.aspect - header_aspect) > tol
