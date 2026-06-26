"""Image comparison for visual-sync.

The real comparator is **resolution-agnostic** (plan M3): after the caller crops
the live and reference regions by fraction, ``score`` resizes BOTH to a canonical
pixel size (the reference's dims) before comparing, so a strat recorded at 2x
Retina still matches a 1x replay. Masking (S1) zeroes volatile sub-rects
(cash/timer/particles) before scoring.

numpy is imported lazily inside the real comparator only, so engine/recorder/
recovery tests can inject :class:`MockComparator` and need no third-party deps
(plan M11/M13/S10).
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional, Protocol

from .config import MatchMethod
from .frame import Frame
from .geometry import Rect


class SceneClass(str, Enum):
    MENU = "menu"
    IN_MATCH = "in_match"
    LOADING = "loading"
    ERROR_DIALOG = "error_dialog"
    DISCONNECTED = "disconnected"
    WRONG_MAP = "wrong_map"
    UNKNOWN = "unknown"


class Comparator(Protocol):
    def score(
        self,
        live: Frame,
        ref: Frame,
        method: MatchMethod = MatchMethod.TM_CCOEFF_NORMED,
        mask: Optional[list[Rect]] = None,
    ) -> float: ...


class MockComparator:
    """Deterministic comparator for tests.

    Default behaviour: score 1.0 when ``live.label == ref.label`` else 0.0. This
    lets tests script "what's on screen" purely via :class:`Frame` labels. Pass
    ``score_fn`` for full control.
    """

    def __init__(
        self,
        score_fn: Optional[Callable[[Frame, Frame, MatchMethod, Optional[list[Rect]]], float]] = None,
    ) -> None:
        self.score_fn = score_fn
        self.calls: list[tuple[str | None, str | None]] = []

    def score(self, live, ref, method=MatchMethod.TM_CCOEFF_NORMED, mask=None) -> float:
        self.calls.append((live.label, ref.label))
        if self.score_fn is not None:
            return float(self.score_fn(live, ref, method, mask))
        return 1.0 if (live.label is not None and live.label == ref.label) else 0.0


class NumpyComparator:
    """Real comparator using numpy (and OpenCV if available, but not required)."""

    def score(self, live, ref, method=MatchMethod.TM_CCOEFF_NORMED, mask=None) -> float:
        import numpy as np  # type: ignore

        a = _to_gray(np, live.as_numpy())
        b = _to_gray(np, ref.as_numpy())
        # Canonical geometry = reference dims (M3): resize live to match ref.
        out_h, out_w = b.shape[0], b.shape[1]
        a = _resize_gray(np, a, out_h, out_w)
        if mask:
            _apply_mask(np, a, mask)
            _apply_mask(np, b, mask)
        m = MatchMethod(method)
        if m in (MatchMethod.TM_CCOEFF_NORMED, MatchMethod.NCC):
            return _ncc(np, a, b)
        if m == MatchMethod.TM_SQDIFF_NORMED:
            return _sqdiff_sim(np, a, b)
        if m == MatchMethod.MSE:
            mse = float(((a - b) ** 2).mean())
            return max(0.0, 1.0 - mse / (255.0 ** 2))
        if m == MatchMethod.SSIM:
            return _ssim(np, a, b)
        if m == MatchMethod.PHASH:
            return _phash_sim(np, a, b)
        return _ncc(np, a, b)


# --- numpy helpers (only called when numpy is present) ---

def _to_gray(np, arr):
    if arr.ndim == 2:
        return arr.astype(np.float64)
    c = arr.shape[2]
    if c >= 3:
        return arr[:, :, :3].astype(np.float64).mean(axis=2)
    return arr[:, :, 0].astype(np.float64)


def _resize_gray(np, a, out_h, out_w):
    in_h, in_w = a.shape
    if (in_h, in_w) == (out_h, out_w):
        return a
    ys = (np.arange(out_h) * in_h / max(1, out_h)).astype(int).clip(0, in_h - 1)
    xs = (np.arange(out_w) * in_w / max(1, out_w)).astype(int).clip(0, in_w - 1)
    return a[ys][:, xs]


def _apply_mask(np, gray, mask: list[Rect]) -> None:
    h, w = gray.shape
    for r in mask:
        x0 = max(0, min(round(r.x * w), w))
        y0 = max(0, min(round(r.y * h), h))
        x1 = max(x0, min(round((r.x + r.w) * w), w))
        y1 = max(y0, min(round((r.y + r.h) * h), h))
        gray[y0:y1, x0:x1] = 0.0


def _ncc(np, a, b) -> float:
    am, bm = a.mean(), b.mean()
    a = a - am
    b = b - bm
    sa = float((a ** 2).sum())
    sb = float((b ** 2).sum())
    if sa == 0.0 and sb == 0.0:
        # both flat (constant): match only if the constant levels agree
        return 1.0 if abs(float(am) - float(bm)) < 1.0 else 0.0
    if sa == 0.0 or sb == 0.0:
        return 0.0  # exactly one is flat -> NOT a match (D10: black != textured)
    val = float((a * b).sum() / np.sqrt(sa * sb))
    return max(0.0, min(1.0, val))


def _sqdiff_sim(np, a, b) -> float:
    num = float(((a - b) ** 2).sum())
    sa = float((a ** 2).sum())
    sb = float((b ** 2).sum())
    if sa == 0.0 and sb == 0.0:
        return 1.0  # both all-zero (identical black)
    if sa == 0.0 or sb == 0.0:
        return max(0.0, 1.0 - num / max(sa, sb))  # one all-zero -> ~0 (D11)
    return max(0.0, 1.0 - num / np.sqrt(sa * sb))


def _ssim(np, a, b) -> float:
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / (
        (mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2)
    )
    return max(0.0, min(1.0, float(s)))


def _dhash_bits(np, gray):
    g = _resize_gray(np, gray, 8, 9)
    return (g[:, 1:] > g[:, :-1]).flatten()


def _phash_sim(np, a, b) -> float:
    # dhash yields an all-False bit vector for ANY uniform frame, so two flat frames of different
    # brightness (e.g. solid black vs solid white) would falsely match at Hamming 0. Mirror _ncc's
    # D10/D11 flat-frame rule before hashing (round 23 #11).
    fa, fb = float(a.std()) == 0.0, float(b.std()) == 0.0
    if fa and fb:
        return 1.0 if abs(float(a.mean()) - float(b.mean())) < 1.0 else 0.0
    if fa or fb:
        return 0.0  # exactly one is flat -> NOT a match (flat != textured)
    ba, bb = _dhash_bits(np, a), _dhash_bits(np, b)
    hamming = int((ba != bb).sum())
    return 1.0 - hamming / float(ba.size)


def make_comparator() -> Comparator:
    return NumpyComparator()


def load_reference(path: str) -> Frame:
    """Load a reference PNG into a Frame.

    Tries Pillow (handles any format), then OpenCV, then the stdlib reader in
    :mod:`pngio` (needs only numpy). The comparator scores on a channel-mean
    grayscale, so RGB-vs-BGR ordering is irrelevant.
    """
    # Each backend: a MISSING backend (ImportError) is skipped; a DECODE failure
    # falls through to the next reader (so the documented chain actually holds).
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore
        _pil = (Image, np)
    except ImportError:
        _pil = None
    if _pil is not None:
        try:
            Image, np = _pil
            return Frame.from_numpy(np.asarray(Image.open(path).convert("RGBA")), label=path)
        except Exception:
            pass  # not decodable by Pillow -> try the next reader
    try:
        import cv2  # type: ignore
        _cv2 = cv2
    except ImportError:
        _cv2 = None
    if _cv2 is not None:
        try:
            arr = _cv2.imread(path, _cv2.IMREAD_UNCHANGED)
            if arr is not None:
                return Frame.from_numpy(arr, label=path)
        except Exception:
            pass
    # stdlib fallback (numpy only) — raises a clear error if this can't read it either
    import numpy as np  # type: ignore
    from .pngio import read_png

    data, w, h, c = read_png(path)
    arr = np.frombuffer(data, dtype=np.uint8).reshape(h, w, c)
    return Frame.from_numpy(arr, label=path)
