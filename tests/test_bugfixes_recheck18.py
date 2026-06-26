"""Regression tests for review round 20 (2 findings, both confirmed genuine):
  #1 config._coerce_type: a declared-nullable field can be cleared back to None via an override
  #2 strat._threshold: a hand-edited threshold outside [0,1] is rejected at parse time
"""

import pytest

from tds_macro import strat as S
from tds_macro.config import Config
from tds_macro.errors import StratValidationError


# --- #1 nullable fields are clearable; non-nullable still reject None ---
def test_with_overrides_can_clear_nullable_field():
    cfg = Config(retina_scale_override=2.0)
    assert cfg.with_overrides({"retina_scale_override": None}).retina_scale_override is None
    cfg2 = Config(window_rect_override=(0, 0, 100, 100))
    assert cfg2.with_overrides({"window_rect_override": None}).window_rect_override is None


def test_with_overrides_rejects_null_on_nonnullable():
    with pytest.raises(ValueError):
        Config().with_overrides({"loop_count": None})


# --- #2 threshold must be in [0,1] (score() is clamped there) ---
def _sync_doc(threshold):
    return {"events": [{"id": 1, "t_ms": 0, "type": "sync_point", "ref_frame": "a.png",
                        "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "threshold": threshold}]}


@pytest.mark.parametrize("bad", [50, -1, 1.5, 100.0])
def test_sync_threshold_out_of_range_rejected(bad):
    with pytest.raises(StratValidationError) as ei:
        S.parse(_sync_doc(bad), check_frames=False)
    assert any("threshold" in p for p in ei.value.problems)


@pytest.mark.parametrize("ok", [0.0, 0.5, 0.95, 1.0])
def test_sync_threshold_in_range_ok(ok):
    st = S.parse(_sync_doc(ok), check_frames=False)
    sp = [e for e in st.events if e.type == "sync_point"][0]
    assert sp.threshold == ok


def test_detector_threshold_out_of_range_rejected():
    doc = {"events": [], "recovery": {"wrong_map": {
        "ref_frame": "f.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "threshold": 5}}}
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    assert any("threshold" in p for p in ei.value.problems)
