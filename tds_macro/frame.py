"""A lightweight screen-frame wrapper.

Per plan M11, the core import graph must stay clean on a box without numpy, so a
Frame stores raw ``bytes`` + ``shape`` by default and only touches numpy lazily,
through :meth:`as_numpy`. The mock capture backend and the engine never need
pixels (they use a MockComparator), so engine/recorder/recovery tests run with
zero third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # numpy is optional for the core; required only by the real comparator
    import numpy as _np  # type: ignore

    HAS_NUMPY = True
except Exception:  # pragma: no cover - exercised on the numpy-less smoke test
    _np = None  # type: ignore
    HAS_NUMPY = False


@dataclass
class Frame:
    """An image: ``shape`` is (height, width, channels); ``data`` is bytes or ndarray.

    Channels are BGRA when produced by the mss backend. ``label`` is an optional
    tag used by the mock backend so tests/recovery can script "which screen is this".
    """

    data: Any
    shape: tuple[int, int, int]
    label: str | None = None

    @property
    def height(self) -> int:
        return self.shape[0]

    @property
    def width(self) -> int:
        return self.shape[1]

    @property
    def channels(self) -> int:
        return self.shape[2]

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @classmethod
    def from_numpy(cls, arr: Any, label: str | None = None) -> "Frame":
        h, w = int(arr.shape[0]), int(arr.shape[1])
        c = int(arr.shape[2]) if arr.ndim == 3 else 1
        return cls(data=arr, shape=(h, w, c), label=label)

    @classmethod
    def from_bytes(cls, data: bytes, shape: tuple[int, int, int], label: str | None = None) -> "Frame":
        return cls(data=data, shape=shape, label=label)

    @classmethod
    def labelled(cls, label: str, shape: tuple[int, int, int] = (4, 4, 4)) -> "Frame":
        """A tiny placeholder frame carrying only a scene label (for mocks/tests)."""
        return cls(data=b"\x00" * (shape[0] * shape[1] * shape[2]), shape=shape, label=label)

    def as_numpy(self) -> Any:
        """Return the frame as an (H,W,C) numpy array. Requires numpy."""
        if not HAS_NUMPY:
            raise RuntimeError(
                "numpy is required for pixel operations on Frame; install with "
                "`pip install numpy`. (The core engine/recorder/recovery do not need it.)"
            )
        if isinstance(self.data, _np.ndarray):
            return self.data
        arr = _np.frombuffer(self.data, dtype=_np.uint8)
        return arr.reshape(self.shape)
