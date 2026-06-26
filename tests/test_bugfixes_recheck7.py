"""Regression tests for the 7th workflow recheck (3 findings; see docs/BUGLOG.md)."""

import struct

import pytest

from tds_macro.pngio import write_png, read_png
from tds_macro.visual import load_reference
from tds_macro.frame import Frame


# #1 a corrupt PNG (IHDR dims > actual scanline data) raises a clear ValueError, not IndexError
def test_read_png_corrupt_raises_clear_error(tmp_path):
    p = str(tmp_path / "small.png")
    write_png(p, bytes([10, 20, 30, 255]) * (2 * 2), 2, 2, 4)
    raw = bytearray(open(p, "rb").read())
    raw[16:20] = struct.pack(">I", 8)   # lie: IHDR width 2 -> 8
    raw[20:24] = struct.pack(">I", 8)   # lie: IHDR height 2 -> 8
    bad = str(tmp_path / "bad.png")
    open(bad, "wb").write(raw)
    with pytest.raises(ValueError) as ei:
        read_png(bad)
    assert "corrupt" in str(ei.value).lower()


# #2 load_reference falls through the whole chain on a decode failure (not just ImportError)
def test_load_reference_fallback_on_decode_failure(tmp_path):
    p = str(tmp_path / "notimage.png")
    open(p, "wb").write(b"definitely not an image")
    # Pillow is installed and raises UnidentifiedImageError; the chain must continue to
    # the stdlib reader, which raises a clear "not a PNG" ValueError (NOT a PIL error).
    with pytest.raises(ValueError):
        load_reference(p)


def test_load_reference_still_loads_valid_png(tmp_path):
    p = str(tmp_path / "ok.png")
    write_png(p, bytes([5, 5, 5, 255]) * (4 * 3), 4, 3, 4)
    f = load_reference(p)
    assert isinstance(f, Frame) and f.size == (4, 3)


# #3 build()/capture before run() doesn't AttributeError on _strat_dir
def test_recorder_build_before_run():
    from tds_macro.recorder import Recorder
    from tds_macro.window import MockWindowProvider
    from tds_macro.input_backend import MockInputBackend
    from tds_macro.capture import MockCaptureBackend
    from tds_macro.hotkeys import HotkeyManager, HotkeyEvents
    from tds_macro.strat import StratFile
    from helpers import mock_config
    rec = Recorder(MockWindowProvider(), MockInputBackend(), MockCaptureBackend(), mock_config(),
                   HotkeyManager(mock_config(), HotkeyEvents()))
    st = rec.build("out.strat.json")  # never called run() first
    assert isinstance(st, StratFile)
