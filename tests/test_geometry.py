"""Geometry: A∘B and C∘D round-trips, monitor-relative physical coords, crop (plan 8.1)."""

from tds_macro.geometry import Coordinates, Point, Rect, WindowGeometry, aspect_mismatch


def _coords(monitor=(0, 0), retina=2.0):
    return Coordinates(WindowGeometry(x=200, y=120, w=1600, h=900, retina=retina,
                                      monitor_x=monitor[0], monitor_y=monitor[1]))


def test_AB_roundtrip():
    c = _coords()
    for p in (Point(0.0, 0.0), Point(1.0, 1.0), Point(0.4123, 0.6310)):
        px, py = c.norm_to_logical(p)
        b = c.logical_to_norm(px, py)
        assert abs(b.x - p.x) < 1e-9 and abs(b.y - p.y) < 1e-9


def test_CD_roundtrip_multimonitor():
    # Window on a monitor that starts at logical x=-1920 (left of main).
    c = _coords(monitor=(-1920, 0))
    c.geo = WindowGeometry(-1920, 50, 1600, 900, 2.0, -1920, 0)
    p = Point(0.3, 0.7)
    ppx, ppy = c.norm_to_physical(p)
    assert ppx >= 0 and ppy >= 0  # monitor-relative, never negative (M6)
    d = c.physical_to_norm(ppx, ppy)
    assert abs(d.x - p.x) < 1e-9 and abs(d.y - p.y) < 1e-9


def test_physical_is_retina_scaled():
    c = _coords(retina=2.0)
    # window at logical (200,120); monitor origin (0,0); point at window origin
    ppx, ppy = c.norm_to_physical(Point(0, 0))
    assert ppx == 200 * 2.0 and ppy == 120 * 2.0


def test_region_crop_box_cancels_retina():
    # The same normalized region maps to proportional pixels at any image size.
    r = Rect(0.43, 0.0, 0.14, 0.07)
    small = Coordinates.region_crop_box(r, 400, 200)
    big = Coordinates.region_crop_box(r, 800, 400)
    assert big[0] == small[0] * 2 and big[2] == small[2] * 2
    assert small[2] > small[0] and small[3] > small[1]


def test_aspect_mismatch():
    g = WindowGeometry(0, 0, 1600, 900)
    assert not aspect_mismatch(g, 1600 / 900, 0.02)
    assert aspect_mismatch(g, 4 / 3, 0.02)


def test_out_of_window():
    c = _coords()
    assert not c.is_out_of_window(Point(0.5, 0.5))
    assert c.is_out_of_window(Point(1.5, 0.5))
