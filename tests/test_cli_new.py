"""Tests for the `new` CLI subcommand — create a fresh, empty strat file (then validate it loads)."""

from types import SimpleNamespace

from tds_macro import cli
from tds_macro import strat as S


def test_new_creates_loadable_blank_strat(tmp_path):
    p = tmp_path / "fresh.strat.json"
    args = cli.build_parser().parse_args(
        ["new", str(p), "--map", "Polluted Wastelands II", "--difficulty", "Fallen"])
    assert args.func(args) == 0
    assert p.exists()
    # the freshly-created file round-trips through the validator with no problems
    st = S.load(str(p))
    assert st.events == [] and st.header.map == "Polluted Wastelands II"
    assert st.header.difficulty == "Fallen"


def test_new_refuses_to_overwrite_without_force(tmp_path):
    p = tmp_path / "keep.strat.json"
    p.write_text("PRECIOUS")
    args = SimpleNamespace(strat=str(p), name=None, map=None, difficulty=None, force=False)
    assert cli.cmd_new(args) == 1
    assert p.read_text() == "PRECIOUS"  # untouched


def test_new_force_overwrites(tmp_path):
    p = tmp_path / "keep.strat.json"
    p.write_text("PRECIOUS")
    args = SimpleNamespace(strat=str(p), name=None, map=None, difficulty=None, force=True)
    assert cli.cmd_new(args) == 0
    assert p.read_text() != "PRECIOUS"  # replaced with a real (loadable) strat
    assert S.load(str(p)).events == []
