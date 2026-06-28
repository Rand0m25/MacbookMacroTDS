"""Sync-point localization: check ALL sync frames to find which checkpoint we're at, then resume
there (opt-in via localize_on_start / localize_on_timeout). Default-off path stays linear."""

from tds_macro import strat as S
from tds_macro.visual import MockComparator
from tds_macro.capture import MockCaptureBackend
from tds_macro.engine import RunState

from helpers import build_player, mock_config, mk_sync


# --------------------------------------------------------------------------- #
# _localize_against_syncs unit tests (the matching/gating logic in isolation)
# --------------------------------------------------------------------------- #
def _scored_player(score_map, **cfg):
    """Player whose comparator scores by ref label (ref.label == the sync's label)."""
    def score_fn(live, ref, method, mask):
        return score_map.get(ref.label, 0.0)
    p, _, _, _ = build_player(S.StratFile(base_dir="."), cfg=mock_config(**cfg),
                              comparator=MockComparator(score_fn=score_fn))
    return p


def _syncs(*specs):
    return [mk_sync(i + 1, (i + 1) * 100, lbl, threshold=thr) for i, (lbl, thr) in enumerate(specs)]


def test_localize_picks_single_best_match():
    prims = _syncs(("s0", 0.9), ("s1", 0.9), ("s2", 0.9))
    p = _scored_player({"s1": 0.97})
    assert p._localize_against_syncs(prims, [0, 1, 2], expected_i=None, live=None) == 1


def test_localize_declines_when_nothing_clears_floor():
    prims = _syncs(("s0", 0.9), ("s1", 0.9))
    p = _scored_player({"s0": 0.5, "s1": 0.6})
    assert p._localize_against_syncs(prims, [0, 1], expected_i=None, live=None) is None


def test_localize_gate_uses_each_syncs_own_threshold():
    # s0 scores 0.90: clears localize_min_score (0.85) but NOT its own 0.95 threshold -> rejected.
    prims = _syncs(("s0", 0.95), ("s1", 0.80))
    p = _scored_player({"s0": 0.90, "s1": 0.0}, localize_min_score=0.85)
    assert p._localize_against_syncs(prims, [0, 1], expected_i=None, live=None) is None


def test_localize_declines_on_ambiguous_margin():
    prims = _syncs(("s0", 0.9), ("s1", 0.9))
    p = _scored_player({"s0": 0.95, "s1": 0.93}, localize_margin=0.05)  # diff 0.02 < margin
    assert p._localize_against_syncs(prims, [0, 1], expected_i=None, live=None) is None


def test_localize_forward_only_by_default_but_rewind_opt_in():
    prims = _syncs(("s0", 0.9), ("s1", 0.9), ("s2", 0.9))
    # only the EARLIER sync matches; we're at index 2 -> a backward jump
    assert _scored_player({"s0": 0.99})._localize_against_syncs(
        prims, [0, 1, 2], expected_i=2, live=None) is None
    assert _scored_player({"s0": 0.99}, localize_allow_rewind=True)._localize_against_syncs(
        prims, [0, 1, 2], expected_i=2, live=None) == 0


def test_localize_skips_expected_and_expect_syncs():
    prims = _syncs(("expect_7", 0.9), ("s1", 0.9))
    p = _scored_player({"expect_7": 0.99, "s1": 0.99})
    # expect_ candidate is skipped; expected_i=1 is excluded -> nothing left -> decline
    assert p._localize_against_syncs(prims, [0, 1], expected_i=1, live=None) is None


# --------------------------------------------------------------------------- #
# _play_sequence integration (the MockComparator default: 1.0 iff labels match)
# --------------------------------------------------------------------------- #
def _keys_dispatched(inp):
    return [e["key"] for e in inp.events if e["action"] == "key_press"]


def _seq():
    return [S.KeyPressEvent(1, 0, "key_press", key="a"),
            mk_sync(2, 100, "s1", on_timeout="continue"),
            S.KeyPressEvent(3, 200, "key_press", key="b"),
            mk_sync(4, 300, "s2", on_timeout="continue"),
            S.KeyPressEvent(5, 400, "key_press", key="c")]


def _play(events, *, current_label, **cfg):
    p, inp, _, _ = build_player(S.StratFile(base_dir=".", events=events), cfg=mock_config(**cfg),
                                capture=MockCaptureBackend(current_label=current_label))
    p._play_sequence(events, RunState.IN_MATCH, localize=True)
    return _keys_dispatched(inp)


def test_localize_on_start_jumps_to_matching_checkpoint():
    # screen reads s2 at start -> resume at s2; the opening (a) and b are skipped
    assert _play(_seq(), current_label="s2", localize_on_start=True) == ["c"]


def test_localize_on_start_declines_keeps_opening():
    # screen matches no sync -> no jump; the whole sequence plays (opening preserved)
    assert _play(_seq(), current_label="nomatch", localize_on_start=True) == ["a", "b", "c"]


def test_localize_on_timeout_jumps_forward():
    # s1 times out (screen reads s2), localizer jumps to s2 -> b skipped
    assert _play(_seq(), current_label="s2", localize_on_timeout=True) == ["a", "c"]


def test_localize_off_is_linear_even_when_screen_matches():
    # flags OFF (default): no jumping regardless of what's on screen -> plain linear replay
    assert _play(_seq(), current_label="s2") == ["a", "b", "c"]


def test_no_syncs_plays_linearly_with_localize_on():
    events = [S.KeyPressEvent(1, 0, "key_press", key="a"),
              S.KeyPressEvent(2, 100, "key_press", key="b")]
    assert _play(events, current_label="x", localize_on_start=True, localize_on_timeout=True) == ["a", "b"]


def test_localize_off_does_not_localize_in_dry_run():
    # dry-run replays linearly (no jumps), mirroring Hook A — even with localize_on_timeout on
    events = _seq()
    p, inp, _, _ = build_player(S.StratFile(base_dir=".", events=events),
                                cfg=mock_config(localize_on_timeout=True, localize_on_start=True, dry_run=True),
                                capture=MockCaptureBackend(current_label="s2"))
    p._play_sequence(events, RunState.IN_MATCH, localize=True)
    assert _keys_dispatched(inp) == []  # dry-run sends nothing, and crucially never jumped


def test_localize_jump_drains_held_keys():
    # a held key whose release lies in the SKIPPED range must not stay physically held after a jump
    events = [S.KeyPressEvent(1, 0, "key_press", key="x"),         # press & hold x
              mk_sync(2, 100, "s1", on_timeout="continue"),
              S.KeyReleaseEvent(3, 150, "key_release", key="x"),   # its release — in the skipped range
              S.KeyPressEvent(4, 200, "key_press", key="a"),
              mk_sync(5, 300, "s2", on_timeout="continue"),
              S.KeyPressEvent(6, 400, "key_press", key="c")]
    p, inp, _, _ = build_player(S.StratFile(base_dir=".", events=events),
                                cfg=mock_config(localize_on_timeout=True),
                                capture=MockCaptureBackend(current_label="s2"))
    p._play_sequence(events, RunState.IN_MATCH, localize=True)
    assert "x" not in inp.held_keys  # released by the jump's release_all, not left stuck down
    assert any(e["action"] == "key_release" and e.get("reason") == "release_all" and e["key"] == "x"
               for e in inp.events)
