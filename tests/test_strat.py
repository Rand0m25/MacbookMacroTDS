"""Strat: validation (M12), round-trip, atomic save (S11), macro expansion (M8)."""

import json
import os

import pytest

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.geometry import Point


def _valid_dict():
    return {
        "schema_version": 1,
        "header": {"map": "PW2", "window_aspect": 1.778},
        "events": [
            {"id": 1, "t_ms": 1500, "type": "place_tower", "tower": "Farm", "hotbar_slot": 1,
             "pos": {"x": 0.41, "y": 0.63}, "settle_ms": 250},
            {"id": 2, "t_ms": 6000, "type": "sync_point", "label": "w5", "ref_frame": "f.png",
             "region": {"x": 0.4, "y": 0, "w": 0.1, "h": 0.07}, "on_timeout": "recover"},
        ],
    }


def test_parse_and_roundtrip():
    st = S.parse(_valid_dict(), check_frames=False)
    assert len(st.events) == 2
    again = S.parse(json.loads(json.dumps(st.to_dict())), check_frames=False)
    assert again.events[0].type == "place_tower"


def test_validation_collects_all_errors():
    bad = {"schema_version": 99, "events": [
        {"id": 1, "t_ms": 1, "type": "click", "pos": {"x": 2.0, "y": 0.5}},
        {"id": 2, "t_ms": 2, "type": "place_tower", "hotbar_slot": 99},
        {"id": 3, "t_ms": 3, "type": "bogus"},
        {"id": 4, "t_ms": 4, "type": "sync_point", "region": {"x": 0, "y": 0, "w": 0.1, "h": 0.1},
         "on_timeout": "nope"},
        {"id": 5, "t_ms": 5, "type": "click", "pos": {"x": 0.5, "y": 0.5}, "wat": 1},  # unknown key
    ]}
    with pytest.raises(StratValidationError) as ei:
        S.parse(bad, check_frames=False)
    probs = ei.value.problems
    assert any("newer than supported" in p for p in probs)
    assert any("outside normalized range" in p for p in probs)
    assert any("hotbar_slot" in p for p in probs)
    assert any("unknown event type" in p for p in probs)
    assert any("on_timeout" in p for p in probs)
    assert any("unknown field" in p for p in probs)


def test_disconnect_reset_action_rejected():  # M17
    d = {"events": [], "recovery": {"disconnect": {
        "ref_frame": "f.png", "region": {"x": 0, "y": 0, "w": 1, "h": 1}, "action": "reset_and_rejoin"}}}
    with pytest.raises(StratValidationError) as ei:
        S.parse(d, check_frames=False)
    assert any("reconnect_and_rejoin" in p for p in ei.value.problems)


def test_expand_macro_absolute_tms_sorted():  # M8
    pt = S.PlaceTowerEvent(2, 1500, "place_tower", tower="Farm", hotbar_slot=1,
                           pos=Point(0.4, 0.6), settle_ms=250)
    prims = S.expand_macro(pt)
    ts = [e.t_ms for e in prims]
    assert ts == sorted(ts) and ts[0] == 1500
    # settle realized as a trailing wait/sync after the placing click
    assert prims[-1].type in ("wait", "sync_point")
    assert any(e.type == "key_press" for e in prims) and any(e.type == "click" for e in prims)


def test_expand_all_is_time_sorted():
    evs = [
        S.UpgradeEvent(1, 5000, "upgrade", target_pos=Point(0.5, 0.5),
                       upgrade_button_pos=Point(0.9, 0.7), times=2, between_ms=300),
        S.PlaceTowerEvent(2, 1000, "place_tower", tower="X", hotbar_slot=1, pos=Point(0.3, 0.3)),
    ]
    prims = S.expand_all(evs)
    assert [e.t_ms for e in prims] == sorted(e.t_ms for e in prims)


def test_atomic_save_and_png_dims(tmp_path):
    from tds_macro.pngio import write_png
    frames = tmp_path / "frames"
    frames.mkdir()
    write_png(str(frames / "f.png"), bytes([10, 20, 30, 255]) * (8 * 6), 8, 6, 4)
    assert S.png_dimensions(str(frames / "f.png")) == (8, 6)

    st = S.parse(_valid_dict(), check_frames=False)
    st.events[1].ref_frame = "frames/f.png"
    path = str(tmp_path / "s.strat.json")
    S.save(st, path)
    assert os.path.exists(path)
    # no leftover temp files (atomic replace)
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    loaded = S.load(path, check_frames=True)  # frame existence + dims validated
    assert loaded.header.map == "PW2"


def test_unknown_top_level_key_rejected():
    with pytest.raises(StratValidationError) as ei:
        S.parse({"events": [], "totally_bogus": 1}, check_frames=False)
    assert any("top level" in p for p in ei.value.problems)


def test_missing_frame_is_caught(tmp_path):
    st = S.parse(_valid_dict(), check_frames=False)
    path = str(tmp_path / "s.json")
    S.save(st, path)
    with pytest.raises(StratValidationError) as ei:
        S.load(path, check_frames=True)
    assert any("not found" in p for p in ei.value.problems)
