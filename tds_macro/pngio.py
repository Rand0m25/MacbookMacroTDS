"""Minimal stdlib PNG writer (zlib only).

Lets the recorder save reference frames and the example generator create frames
without requiring Pillow/OpenCV/numpy. Writes 8-bit RGB or RGBA. Atomic
(temp-in-same-dir + fsync + os.replace, plan S11).
"""

from __future__ import annotations

import os
import struct
import tempfile
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: str, data: bytes, width: int, height: int, channels: int = 4) -> None:
    """Write row-major 8-bit pixel ``data`` (RGB or RGBA) to a PNG atomically."""
    if channels not in (3, 4):
        raise ValueError("channels must be 3 (RGB) or 4 (RGBA)")
    expected = width * height * channels
    if len(data) != expected:
        raise ValueError(f"data length {len(data)} != width*height*channels {expected}")

    color_type = 6 if channels == 4 else 2
    stride = width * channels
    # prepend filter byte 0 to each scanline
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += data[y * stride:(y + 1) * stride]

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")

    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path: str) -> tuple[bytes, int, int, int]:
    """Decode an 8-bit RGB/RGBA, non-interlaced PNG with stdlib only.

    Returns (pixel_bytes, width, height, channels). Handles all 5 scanline
    filter types. Good enough for reference frames written by this tool or any
    standard PNG; for exotic formats install Pillow.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = bit_depth = color_type = interlace = 0
    idat = bytearray()
    while pos < len(raw):
        if pos + 12 > len(raw):  # need 4 len + 4 tag + ... + 4 crc; reject truncated headers cleanly
            raise ValueError("corrupt PNG: truncated chunk header")
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        tag = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if pos + 12 + length > len(raw):  # chunk body runs past EOF
            raise ValueError("corrupt PNG: truncated chunk body")
        pos += 12 + length  # length + tag + data + crc
        if tag == b"IHDR":
            if len(data) != 13:  # IHDR is exactly 13 bytes; a wrong length -> clean ValueError, not struct.error (round 22 #D)
                raise ValueError("corrupt PNG: bad IHDR length")
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        elif tag == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
        raise ValueError(f"unsupported PNG (bit_depth={bit_depth}, color_type={color_type}, "
                         f"interlace={interlace}); install Pillow for this image")
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    try:
        decompressed = zlib.decompress(bytes(idat))
    except zlib.error as e:  # zero/garbage IDAT -> clean ValueError like every other path (round 22c #3)
        raise ValueError(f"corrupt PNG: bad IDAT/deflate stream ({e})") from e
    if len(decompressed) < (stride + 1) * height:  # IHDR dims vs actual scanline data
        raise ValueError("corrupt PNG: scanline data shorter than the IHDR dimensions declare")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        ftype = decompressed[src]; src += 1
        line = bytearray(decompressed[src:src + stride]); src += stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            x = line[i]
            if ftype == 1:
                x += a
            elif ftype == 2:
                x += b
            elif ftype == 3:
                x += (a + b) >> 1
            elif ftype == 4:
                x += _paeth(a, b, c)
            line[i] = x & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return (bytes(out), width, height, channels)


def frame_to_rgba_bytes(frame) -> tuple[bytes, int, int]:
    """Convert a (BGRA/BGR) :class:`Frame` to RGBA bytes for write_png."""
    arr = frame.as_numpy()  # requires numpy at runtime (mac capture path)
    import numpy as np  # type: ignore

    h, w = arr.shape[0], arr.shape[1]
    c = arr.shape[2] if arr.ndim == 3 else 1
    if c >= 4:
        rgba = arr[:, :, [2, 1, 0, 3]]
    elif c == 3:
        rgb = arr[:, :, [2, 1, 0]]
        alpha = np.full((h, w, 1), 255, dtype=arr.dtype)
        rgba = np.concatenate([rgb, alpha], axis=2)
    else:
        g = arr.reshape(h, w, 1)
        rgba = np.concatenate([g, g, g, np.full((h, w, 1), 255, dtype=arr.dtype)], axis=2)
    return (rgba.astype("uint8").tobytes(), w, h)
