"""Proactive blast-radius tests: the 'raw JSON value used numerically' class,
extended to the Header fields the reviewers hadn't explicitly covered."""

import pytest

from tds_macro import strat as S


@pytest.mark.parametrize("bad", ["wide", [1.7], {"x": 1}, None, True, float("nan"), float("inf")])
def test_malformed_window_aspect_coerced_not_crash(bad):
    st = S.parse({"header": {"window_aspect": bad}, "events": []}, check_frames=False)
    assert st.header.window_aspect == 0.0  # coerced to a safe default, no crash downstream
    assert isinstance(st.header.window_aspect, float)


def test_malformed_retina_and_resolution_coerced():
    st = S.parse({"header": {"retina_scale_captured_at": "two", "reference_resolution": "nope"},
                  "events": []}, check_frames=False)
    assert st.header.retina_scale_captured_at == 1.0
    assert st.header.reference_resolution == {}


def test_valid_header_numbers_preserved():
    st = S.parse({"header": {"window_aspect": 1.7778, "retina_scale_captured_at": 2.0,
                             "reference_resolution": {"w": 2560, "h": 1440}}, "events": []},
                 check_frames=False)
    assert st.header.window_aspect == 1.7778
    assert st.header.retina_scale_captured_at == 2.0
    assert st.header.reference_resolution == {"w": 2560, "h": 1440}


def test_calibrate_does_not_crash_on_coerced_header(tmp_path):
    # End-to-end: a strat whose header had a bad aspect must not crash calibrate's
    # f"{...:.3f}" / abs() — coercion makes header.window_aspect a real float.
    from tds_macro.cli import build_parser
    import json
    p = tmp_path / "s.strat.json"
    p.write_text(json.dumps({"header": {"window_aspect": "huge"}, "events": []}))
    args = build_parser().parse_args(["calibrate", str(p), "--mock", "--no-frames"])
    assert args.func(args) == 0  # runs cleanly, no traceback
