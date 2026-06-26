"""Round 22d — systematic validation pass: `_num` gained lo/hi bounds and every numeric event field
now passes its range through that single helper. These lock in the previously-unbounded gaps so the
whole "field X isn't bounded like its siblings" class stays closed."""

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError


def _raises(doc):
    with pytest.raises(StratValidationError) as ei:
        S.parse(doc, check_frames=False)
    return ei.value.problems


def _ev(**kw):
    base = {"id": 1, "t_ms": 0}
    base.update(kw)
    return {"events": [base]}


@pytest.mark.parametrize("doc,field", [
    (_ev(type="sync_point", ref_frame="a.png", region={"x": 0, "y": 0, "w": 1, "h": 1}, poll_ms=0), "poll_ms"),
    (_ev(type="sync_point", ref_frame="a.png", region={"x": 0, "y": 0, "w": 1, "h": 1}, poll_ms=-5), "poll_ms"),
    (_ev(type="click", pos={"x": 0.5, "y": 0.5}, hold_ms=-1), "hold_ms"),
    (_ev(type="mouse_move", pos={"x": 0.5, "y": 0.5}, duration_ms=-100), "duration_ms"),
    (_ev(type="drag", **{"from": {"x": 0, "y": 0}, "to": {"x": 1, "y": 1}}, duration_ms=-1), "duration_ms"),
    ({"events": [{"id": -3, "t_ms": 0, "type": "wait", "duration_ms": 0}]}, "id"),
])
def test_previously_unbounded_fields_now_rejected(doc, field):
    assert any(field in p for p in _raises(doc))


def test_valid_boundary_values_still_accepted():
    # 0 is valid for durations/settle/t_ms; 1..8 / 1..50 / 1..20 edges; an instant move (0ms) is fine
    doc = {"events": [
        {"id": 0, "t_ms": 0, "type": "mouse_move", "pos": {"x": 0.5, "y": 0.5}, "duration_ms": 0},
        {"id": 1, "t_ms": 0, "type": "click", "pos": {"x": 0.5, "y": 0.5}, "clicks": 20, "hold_ms": 0},
        {"id": 2, "t_ms": 0, "type": "place_tower", "tower": "x", "hotbar_slot": 8,
         "pos": {"x": 0.5, "y": 0.5}, "settle_ms": 0},
        {"id": 3, "t_ms": 0, "type": "upgrade", "target_pos": {"x": 0.5, "y": 0.5},
         "upgrade_button_pos": {"x": 0.9, "y": 0.7}, "times": 50, "between_ms": 0},
    ]}
    st = S.parse(doc, check_frames=False)   # must NOT raise
    assert len(st.events) == 4
