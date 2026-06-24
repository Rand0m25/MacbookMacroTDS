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


@pytest.mark.parametrize("method", list(MatchMethod))
def test_identical_scores_high_all_methods(method):
    a = _rand(32, 32, 7)
    c = NumpyComparator()
    assert c.score(_frame(a), _frame(a), method) > 0.95
