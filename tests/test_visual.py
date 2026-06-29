"""Comparator tests. The real comparator path is numpy-gated (plan S10)."""

import pytest

from tds_macro.config import MatchMethod
from tds_macro.frame import Frame
from tds_macro.geometry import Rect
from tds_macro.visual import MockComparator

np = pytest.importorskip("numpy")
from tds_macro.visual import NumpyComparator  # noqa: E402


def _frame(arr):
    return Frame.from_numpy(arr.astype("uint8"))


def _rand(h, w, seed):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, size=(h, w, 4), dtype=np.uint8)
    a[:, :, 3] = 255
    return a


def test_mock_label_equality():
    c = MockComparator()
    assert c.score(Frame.labelled("a"), Frame.labelled("a")) == 1.0
    assert c.score(Frame.labelled("a"), Frame.labelled("b")) == 0.0


def test_resolution_agnostic_2x_vs_1x():  # M3
    base = _rand(40, 40, 0)
    big = np.repeat(np.repeat(base, 2, axis=0), 2, axis=1)  # same content at 2x Retina
    c = NumpyComparator()
    assert c.score(_frame(big), _frame(base), MatchMethod.TM_CCOEFF_NORMED) > 0.95


def test_different_content_low_score():
    c = NumpyComparator()
    assert c.score(_frame(_rand(40, 40, 1)), _frame(_rand(40, 40, 2)),
                   MatchMethod.TM_CCOEFF_NORMED) < 0.3


def test_masking_restores_score():  # S1
    base = _rand(40, 40, 3)
    corrupt = base.copy()
    corrupt[0:20, 0:20, :] = 0
    c = NumpyComparator()
    no_mask = c.score(_frame(corrupt), _frame(base), MatchMethod.MSE)
    masked = c.score(_frame(corrupt), _frame(base), MatchMethod.MSE, mask=[Rect(0, 0, 0.5, 0.5)])
    assert masked > no_mask


def test_mask_excludes_not_zeroes_so_wrong_unmasked_screen_stays_low():
    # The masked (top 80%) region is volatile/different; the UNMASKED bottom genuinely differs (50 vs
    # 200). Excluding the mask (not zeroing it) must keep the score LOW — the old zeroing made both
    # frames agree on the big masked region and inflated NCC toward a false match (round 26 #10).
    W = 40
    rng = np.random.default_rng(1)
    top_ref, top_live = rng.integers(0, 256, (32, W)), rng.integers(0, 256, (32, W))
    ref = np.vstack([top_ref, np.full((8, W), 50)])
    live = np.vstack([top_live, np.full((8, W), 200)])
    G = lambda a: _frame(np.repeat(a[:, :, None], 3, axis=2))  # noqa: E731
    c = NumpyComparator()
    score = c.score(G(live), G(ref), MatchMethod.TM_CCOEFF_NORMED, mask=[Rect(0, 0, 1.0, 0.8)])
    assert score < 0.9  # only the differing bottom is compared -> not a match


def test_full_mask_is_not_a_match():
    a = _rand(20, 20, 5)
    c = NumpyComparator()
    assert c.score(_frame(a), _frame(a), MatchMethod.TM_CCOEFF_NORMED, mask=[Rect(0, 0, 1, 1)]) == 0.0


@pytest.mark.parametrize("method", list(MatchMethod))
def test_identical_scores_high_all_methods(method):
    a = _rand(32, 32, 7)
    c = NumpyComparator()
    assert c.score(_frame(a), _frame(a), method) > 0.95


def test_ssim_full_mask_returns_zero_not_false_match():
    # a mask covering the whole region zeroes BOTH frames; SSIM/pHash of two all-zero arrays used to
    # return 1.0 (false match on any screen). Now it returns 0.0 like the pixel-statistical metrics.
    a = _frame(np.zeros((40, 40, 4)))
    b = _frame(np.full((40, 40, 4), 255))
    for method in (MatchMethod.SSIM, MatchMethod.PHASH):
        assert NumpyComparator().score(a, b, method, mask=[Rect(0, 0, 1, 1)]) == 0.0
