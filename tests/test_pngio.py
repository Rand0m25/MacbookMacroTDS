"""Stdlib PNG writer/reader round-trip (no numpy needed)."""

import os

from tds_macro.pngio import read_png, write_png
from tds_macro.strat import png_dimensions


def test_write_read_roundtrip_rgba(tmp_path):
    w, h, c = 9, 7, 4
    px = bytes((i * 7) % 256 for i in range(w * h * c))
    p = str(tmp_path / "a.png")
    write_png(p, px, w, h, c)
    assert png_dimensions(p) == (w, h)
    data, rw, rh, rc = read_png(p)
    assert (rw, rh, rc) == (w, h, c)
    assert data == px


def test_write_read_roundtrip_rgb(tmp_path):
    w, h, c = 5, 4, 3
    px = bytes((i * 13) % 256 for i in range(w * h * c))
    p = str(tmp_path / "b.png")
    write_png(p, px, w, h, c)
    data, rw, rh, rc = read_png(p)
    assert (rw, rh, rc) == (w, h, c) and data == px


def test_atomic_no_temp_left(tmp_path):
    write_png(str(tmp_path / "c.png"), bytes(4 * 2 * 2), 2, 2, 4)
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
