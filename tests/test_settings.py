"""Persisted GUI settings store (tds_macro/settings.py)."""

import json

from tds_macro import settings as S
from tds_macro.config import Config


def test_defaults_match_config():
    d, c = S.defaults(), Config()
    assert set(d) == set(S.FIELDS)
    for f in S.FIELDS:
        assert d[f] == getattr(c, f)


def test_load_missing_returns_empty(tmp_path):
    assert S.load(str(tmp_path / "nope.json")) == {}


def test_load_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    assert S.load(str(p)) == {}


def test_load_non_object_returns_empty(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]")
    assert S.load(str(p)) == {}


def test_save_load_roundtrip_filters_unknown_keys(tmp_path):
    p = str(tmp_path / "s.json")
    S.save({"panic_hotkey": "f6", "jitter_ms": 25, "bogus": 1}, p)
    assert S.load(p) == {"panic_hotkey": "f6", "jitter_ms": 25}
    assert "bogus" not in json.loads(open(p).read())  # stray key dropped on write too


def test_validate_ok():
    assert S.validate({"jitter_ms": 30, "verify_foreground": False, "localize_min_score": 0.9}) == []


def test_validate_reports_bad_coercion_per_field():
    assert any("jitter_ms" in p for p in S.validate({"jitter_ms": "abc"}))


def test_validate_reports_out_of_bounds():
    assert any("localize_min_score" in p for p in S.validate({"localize_min_score": 9.0}))


def test_validate_rejects_bool_for_int_field():
    assert S.validate({"sync_poll_ms": True})  # bool is not a valid int override


def test_validate_accepts_numeric_strings_from_entry_widgets():
    # the Settings window passes Entry text as strings; with_overrides coerces "50" -> 50
    assert S.validate({"jitter_ms": "50", "sync_default_threshold": "0.88"}) == []
