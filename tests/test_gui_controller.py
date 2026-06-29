"""Unit tests for the GUI controller — verifies each button/action does what it's intended,
with no Tk/display needed (the controller is Tk-free; only run_gui imports tkinter)."""

import threading
import time

from tds_macro import strat as S
from tds_macro.errors import StratValidationError
from tds_macro.engine import RunStats, RunState
from tds_macro.hotkeys import HotkeyEvents
from tds_macro.gui import GuiController, GuiDeps

from helpers import mock_config


class FakeHK:
    def __init__(self):
        self.events = HotkeyEvents()
        self.started = False
        self.stopped = False

    def should_abort(self):
        return self.events.should_abort()

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakePlayer:
    def __init__(self, hk):
        self.hk = hk
        self.stats = RunStats()
        self.state = RunState.IDLE
        self.started = threading.Event()

    def run(self):
        self.started.set()
        self.state = RunState.IN_MATCH
        self.stats.runs = 1
        while not self.hk.should_abort():  # block until Stop sets panic/stop
            time.sleep(0.002)
        self.state = RunState.STOPPING


class FakeRecorder:
    def __init__(self, hk):
        self.hk = hk
        self.started = threading.Event()

    def run(self, path, header=None):
        self.started.set()
        while not self.hk.events.is_stop():
            time.sleep(0.002)
        return S.StratFile(base_dir=".", header=header or S.Header())


def _setup(*, consent=True, load=None, **over):
    h = {"saved": [], "cfg_kwargs": {}, "consent": consent}

    def build_config(**kw):
        h["cfg_kwargs"] = kw
        c = mock_config()
        if kw.get("loop_count") is not None:
            c.loop_count = kw["loop_count"]
        if kw.get("dry_run") is not None:
            c.dry_run = kw["dry_run"]
        if kw.get("private_server"):
            c.private_server_url = kw["private_server"]
        return c

    def make_play(st, cfg):
        hk = FakeHK()
        h["player"], h["hk"], h["cfg"] = FakePlayer(hk), hk, cfg
        return h["player"], hk

    def make_record(cfg):
        hk = FakeHK()
        h["recorder"], h["hk"], h["cfg"] = FakeRecorder(hk), hk, cfg
        return h["recorder"], hk

    deps = GuiDeps(
        build_config=build_config, make_play_engine=make_play, make_record_engine=make_record,
        load_strat=load or (lambda p: S.StratFile(base_dir=".")),
        save_strat=lambda strat, path: h["saved"].append((strat, path)),
        consent_ok=lambda: h["consent"], set_consent=lambda: h.__setitem__("consent", True),
        make_header=lambda n, m, d: S.Header(name=n, map=m, difficulty=d),
        # hermetic settings deps so tests never touch the real ~/.tds_macro_settings.json
        load_settings=lambda: {}, save_settings=lambda v: h["saved_settings"].append(v),
        validate_settings=lambda v: [], settings_defaults=lambda: {},
    )
    h["saved_settings"] = []
    for k, v in over.items():
        setattr(deps, k, v)
    events = []
    ctrl = GuiController(deps, on_event=lambda k, p=None: events.append((k, p)))
    return ctrl, h, events


# -- Validate --
def test_validate_ok():
    ctrl, _, _ = _setup()
    assert ctrl.validate("x.json") == (True, [])


def test_validate_reports_problems():
    def bad(_p):
        raise StratValidationError(["boom", "bad field"])
    ctrl, _, _ = _setup(load=bad)
    ok, problems = ctrl.validate("x.json")
    assert ok is False and "boom" in problems


# -- New (create a blank strat file instead of choosing one) --
def test_new_strat_creates_and_saves_blank_file():
    ctrl, h, events = _setup()
    assert ctrl.new_strat("fresh.strat.json", name="n", map="m", difficulty="d") is True
    assert h["saved"], "expected the blank strat to be saved"
    strat, path = h["saved"][0]
    assert path == "fresh.strat.json"
    assert strat.events == [] and strat.header.map == "m" and strat.header.difficulty == "d"
    assert any(k == "log" for k, _ in events)


def test_new_strat_rejects_empty_path():
    ctrl, h, events = _setup()
    assert ctrl.new_strat("") is False
    assert ctrl.new_strat("   ") is False
    assert not h["saved"]  # nothing written
    assert any(k == "error" for k, _ in events)


def test_new_strat_expands_tilde():
    import os
    ctrl, h, _ = _setup()
    assert ctrl.new_strat("~/fresh.strat.json") is True
    _, path = h["saved"][0]
    assert path == os.path.expanduser("~/fresh.strat.json") and "~" not in path


def test_new_strat_blocked_while_busy():
    ctrl, h, events = _setup()
    assert ctrl.start_play("x.json") is True
    assert h["player"].started.wait(1.0)
    try:
        assert ctrl.new_strat("fresh.strat.json") is False  # don't clobber the file play is using
        assert any(k == "error" for k, _ in events)
    finally:
        ctrl.stop()


def test_new_strat_reports_save_failure():
    def boom(strat, path):
        raise OSError("read-only filesystem")
    ctrl, _, events = _setup(save_strat=boom)
    assert ctrl.new_strat("fresh.strat.json") is False
    assert any(k == "error" for k, _ in events)


# -- Play --
def test_play_starts_and_stop_ends_it():
    ctrl, h, _ = _setup()
    assert ctrl.start_play("x.json") is True
    assert h["player"].started.wait(1.0)
    assert ctrl.is_busy() and ctrl.status()["runs"] == 1
    ctrl.stop()
    assert not ctrl.is_busy()
    assert h["hk"].events.panic.is_set() and h["hk"].stopped
    assert h["player"].state == RunState.STOPPING


def test_play_passes_options():
    ctrl, h, _ = _setup()
    ctrl.start_play("x.json", loop_count=5, dry_run=True, private_server="roblox://x")
    try:
        assert h["cfg"].loop_count == 5 and h["cfg"].dry_run is True
        assert h["cfg"].private_server_url == "roblox://x"
    finally:
        ctrl.stop()


def test_play_requires_consent():
    ctrl, _, events = _setup(consent=False)
    assert ctrl.start_play("x.json", accept_ban_risk=False) is False
    assert ("consent_required", None) in events
    # accepting the ban risk persists consent and lets it start
    assert ctrl.start_play("x.json", accept_ban_risk=True) is True
    ctrl.stop()


def test_busy_guard_blocks_second_activity():
    ctrl, h, events = _setup()
    assert ctrl.start_play("x.json") is True
    assert h["player"].started.wait(1.0)
    assert ctrl.start_play("x.json") is False
    assert ctrl.start_record("y.json") is False
    assert any(k == "error" for k, _ in events)
    ctrl.stop()


def _wait_done(events, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not any(k == "done" for k, _ in events):
        time.sleep(0.002)


class _ImmediatePlayer:
    """A Player whose run() returns at once, leaving preset stats — to drive _run's post-run handling."""
    def __init__(self, runs, reason):
        self.stats = RunStats()
        self.stats.runs = runs
        self.stats.stopped_reason = reason
        self.state = RunState.STOPPING

    def run(self):
        return


def test_play_surfaces_startup_failure_as_error():
    # cold-start launch that never got a window: run() returns normally with runs == 0 + 'error:' reason;
    # the GUI must surface that as an error, not a silent "play finished" (review round 24 #2).
    ctrl, _, events = _setup()
    ctrl.deps.make_play_engine = lambda st, cfg: (
        _ImmediatePlayer(0, "error: WindowNotFoundError: no window"), FakeHK())
    assert ctrl.start_play("x.json") is True
    _wait_done(events)
    assert any(k == "error" and "play failed" in str(p) for k, p in events)


def test_play_surfaces_recovery_giveup_as_error():
    # a zero-match recovery STOP must also be surfaced, not just 'error:' reasons (review round 24 #B)
    ctrl, _, events = _setup()
    ctrl.deps.make_play_engine = lambda st, cfg: (
        _ImmediatePlayer(0, "recovery stopped on wrong_map"), FakeHK())
    assert ctrl.start_play("x.json") is True
    _wait_done(events)
    assert any(k == "error" and "play failed" in str(p) for k, p in events)


def test_play_normal_completion_emits_no_spurious_error():
    # a clean run (a match completed) must NOT be reported as a failure
    ctrl, _, events = _setup()
    ctrl.deps.make_play_engine = lambda st, cfg: (
        _ImmediatePlayer(2, "loop_count reached"), FakeHK())
    assert ctrl.start_play("x.json") is True
    _wait_done(events)
    assert not any(k == "error" for k, _ in events)


def test_play_session_cap_zero_runs_emits_no_error():
    # a configured session-cap stop is benign even with zero matches -> no spurious error
    ctrl, _, events = _setup()
    ctrl.deps.make_play_engine = lambda st, cfg: (
        _ImmediatePlayer(0, "session cap reached"), FakeHK())
    assert ctrl.start_play("x.json") is True
    _wait_done(events)
    assert not any(k == "error" for k, _ in events)


# -- Record --
def test_record_starts_and_saves_on_stop():
    ctrl, h, _ = _setup()
    assert ctrl.start_record("out.json", name="n", map="m", difficulty="d") is True
    assert h["recorder"].started.wait(1.0)
    assert ctrl.status()["activity"] == "record"
    ctrl.stop()
    assert not ctrl.is_busy()
    assert h["saved"] and h["saved"][0][1] == "out.json"


def test_record_uses_private_server():
    ctrl, h, _ = _setup()
    ctrl.start_record("out.json", private_server="roblox://srv")
    try:
        assert h["cfg_kwargs"].get("private_server") == "roblox://srv"
    finally:
        ctrl.stop()


# -- Record into join_sequence / leave_reset_sequence --
class _SeqRecorder:
    """A recorder whose run() returns a StratFile carrying preset `events`."""
    def __init__(self, hk, events):
        self.hk = hk
        self.events = events
        self.started = threading.Event()

    def run(self, path, header=None):
        self.started.set()
        while not self.hk.events.is_stop():
            time.sleep(0.002)
        return S.StratFile(base_dir=".", header=header or S.Header(), events=list(self.events))


def _record_target_setup(recorded, **over):
    """_setup wired so make_record_engine returns a _SeqRecorder yielding `recorded`."""
    box = {}

    def make_record(cfg):
        hk = FakeHK()
        box["rec"] = _SeqRecorder(hk, recorded)
        return box["rec"], hk
    ctrl, h, events = _setup(make_record_engine=make_record, **over)
    return ctrl, h, events, box


def _run_record(ctrl, box, path, **kw):
    assert ctrl.start_record(path, **kw) is True
    assert box["rec"].started.wait(1.0)
    ctrl.stop()  # joins the worker, so the merge+save have completed by the time this returns


def test_record_into_join_merges_and_preserves_main_timeline():
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="space"),
                S.KeyPressEvent(2, 50, "key_press", key="return")]
    base = S.StratFile(base_dir=".", header=S.Header(map="PW2"),
                       events=[S.KeyPressEvent(9, 0, "key_press", key="z")])  # existing main timeline
    ctrl, h, events, box = _record_target_setup(recorded, load_strat_lenient=lambda p: base)
    _run_record(ctrl, box, "main.strat.json", target="join")
    out, path = h["saved"][0]
    assert path == "main.strat.json"
    assert [e.key for e in out.join_sequence] == ["space", "return"]  # recording -> join_sequence
    assert [e.key for e in out.events] == ["z"]                       # existing timeline untouched
    assert out.header.map == "PW2"                                    # existing header preserved


def test_record_into_leave_strips_sync_points():
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="esc"),
                S.SyncPointEvent(2, 50, "sync_point", label="x", ref_frame="x.png"),
                S.ClickEvent(3, 100, "click", button="left", pos=S.Point(0.5, 0.5))]
    ctrl, h, events, box = _record_target_setup(recorded, load_strat_lenient=lambda p: S.StratFile(base_dir="."))
    _run_record(ctrl, box, "m.strat.json", target="leave")
    out, _ = h["saved"][0]
    assert [e.type for e in out.leave_reset_sequence] == ["key_press", "click"]  # sync_point stripped
    assert any(k == "log" and "stripped 1 sync point" in str(p) for k, p in events)


def test_record_into_events_is_unchanged_and_skips_merge_load():
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="a")]

    def boom(p):
        raise AssertionError("load_strat_lenient must not be called for the main-timeline target")
    ctrl, h, events, box = _record_target_setup(recorded, load_strat_lenient=boom)
    _run_record(ctrl, box, "m.strat.json", target="events")
    out, _ = h["saved"][0]
    assert [e.key for e in out.events] == ["a"]
    assert not out.join_sequence and not out.leave_reset_sequence


def test_record_into_join_without_existing_file_creates_fresh():
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="space")]

    def missing(p):
        raise FileNotFoundError(p)
    ctrl, h, events, box = _record_target_setup(recorded, load_strat_lenient=missing)
    _run_record(ctrl, box, "fresh.strat.json", target="join")
    out, _ = h["saved"][0]
    assert [e.key for e in out.join_sequence] == ["space"]
    assert out.events == []  # the recording went to join_sequence, not the main timeline


def test_record_into_join_does_not_destroy_unparseable_existing_file():
    # a file that EXISTS but won't parse (any non-frame validation error) must NOT be overwritten —
    # that would wipe the user's main timeline/recovery. Save the recording to a fallback instead
    # and surface an error (review round 24 #A).
    from tds_macro.errors import StratValidationError
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="space")]

    def invalid(p):
        raise StratValidationError(["coord out of range"])  # present-but-invalid existing strat
    ctrl, h, events, box = _record_target_setup(recorded, load_strat_lenient=invalid)
    _run_record(ctrl, box, "existing.strat.json", target="join")
    assert h["saved"], "the recording must still be saved somewhere (never lost)"
    assert all(path != "existing.strat.json" for _, path in h["saved"])  # original left untouched
    assert any(k == "error" and "left untouched" in str(p) for k, p in events)


def test_record_into_leave_save_failure_falls_back_without_touching_path():
    # if the merged strat can't be written to the chosen path, save a copy elsewhere (don't lose it)
    recorded = [S.KeyPressEvent(1, 0, "key_press", key="esc")]
    saved = []

    def failing_save(strat, path):
        if path == "ro.strat.json":
            raise OSError("read-only filesystem")
        saved.append((strat, path))
    ctrl, h, events, box = _record_target_setup(
        recorded, load_strat_lenient=lambda p: S.StratFile(base_dir="."), save_strat=failing_save)
    _run_record(ctrl, box, "ro.strat.json", target="leave")
    assert saved and all(path != "ro.strat.json" for _, path in saved)  # fell back to a temp file
    assert any(k == "error" and "could not save" in str(p) for k, p in events)


def test_start_record_rejects_unknown_target():
    ctrl, h, events = _setup()
    assert ctrl.start_record("m.strat.json", target="bogus") is False
    assert not ctrl.is_busy() and any(k == "error" for k, _ in events)


# -- Settings (persisted Config-override subset) --
def test_settings_load_into_effective():
    ctrl, _, _ = _setup(load_settings=lambda: {"jitter_ms": 99},
                        settings_defaults=lambda: {"jitter_ms": 0, "verify_foreground": True})
    eff = ctrl.effective_settings()
    assert eff["jitter_ms"] == 99 and eff["verify_foreground"] is True  # defaults + saved overrides


def test_settings_applied_as_overrides_and_strat_wins():
    ctrl, h, _ = _setup(load_settings=lambda: {"jitter_ms": 99, "sync_poll_ms": 5},
                        settings_defaults=lambda: {"jitter_ms": 0, "sync_poll_ms": 1},
                        load=lambda p: S.StratFile(base_dir=".", config_overrides={"sync_poll_ms": 7}))
    ctrl.start_play("x.json")
    try:
        ov = h["cfg_kwargs"]["overrides"]
        assert ov["jitter_ms"] == 99   # user setting reaches the config build
        assert ov["sync_poll_ms"] == 7  # a strat's config_overrides win over the setting
    finally:
        ctrl.stop()


def test_save_settings_validates_persists_and_applies():
    ctrl, h, _ = _setup(settings_defaults=lambda: {"jitter_ms": 0})
    ok, problems = ctrl.save_settings({"jitter_ms": 50})
    assert ok and not problems
    assert ctrl.effective_settings()["jitter_ms"] == 50
    assert h["saved_settings"][-1]["jitter_ms"] == 50


def test_save_settings_rejects_invalid_without_persisting():
    ctrl, h, _ = _setup(settings_defaults=lambda: {"jitter_ms": 0},
                        validate_settings=lambda v: ["jitter_ms: bad"])
    ok, problems = ctrl.save_settings({"jitter_ms": "x"})
    assert ok is False and "jitter_ms: bad" in problems
    assert not h["saved_settings"]  # nothing persisted
    assert ctrl.effective_settings()["jitter_ms"] == 0  # unchanged


def test_save_settings_blocked_while_busy():
    ctrl, h, _ = _setup(settings_defaults=lambda: {"jitter_ms": 0})
    assert ctrl.start_play("x.json") is True
    assert h["player"].started.wait(1.0)
    try:
        ok, problems = ctrl.save_settings({"jitter_ms": 50})
        assert ok is False and problems
    finally:
        ctrl.stop()


def test_reset_settings_restores_defaults():
    ctrl, h, _ = _setup(load_settings=lambda: {"jitter_ms": 99}, settings_defaults=lambda: {"jitter_ms": 0})
    assert ctrl.effective_settings()["jitter_ms"] == 99
    ok, _ = ctrl.reset_settings()
    assert ok and ctrl.effective_settings()["jitter_ms"] == 0
    assert h["saved_settings"][-1]["jitter_ms"] == 0


def test_corrupt_settings_file_degrades_to_defaults():
    def boom():
        raise ValueError("corrupt")
    ctrl, _, _ = _setup(load_settings=boom, settings_defaults=lambda: {"jitter_ms": 7})
    assert ctrl.effective_settings()["jitter_ms"] == 7  # fell back to defaults, didn't crash


# -- Pause / Stop --
def test_pause_toggle_flips_event():
    ctrl, h, _ = _setup()
    ctrl.start_play("x.json")
    h["player"].started.wait(1.0)
    try:
        assert ctrl.pause_toggle() is True and h["hk"].events.pause.is_set()
        assert ctrl.pause_toggle() is False and not h["hk"].events.pause.is_set()
    finally:
        ctrl.stop()


def test_stop_idempotent_when_idle():
    ctrl, _, _ = _setup()
    assert ctrl.stop() is True  # nothing running -> safe no-op
    assert not ctrl.is_busy()


def test_status_idle():
    ctrl, _, _ = _setup()
    s = ctrl.status()
    assert s["busy"] is False and s["activity"] == "idle"


# -- empty / ~ strat path ("couldn't find the directory to save it to") --
def test_start_record_rejects_empty_path():
    ctrl, h, events = _setup()
    assert ctrl.start_record("") is False
    assert ctrl.start_record("   ") is False
    assert not ctrl.is_busy()
    assert h.get("recorder") is None  # engine never built, nothing recorded/lost
    assert any(k == "error" for k, _ in events)


def test_start_play_rejects_empty_path():
    ctrl, h, events = _setup()
    assert ctrl.start_play("") is False
    assert not ctrl.is_busy()
    assert h.get("player") is None
    assert any(k == "error" for k, _ in events)


def test_validate_rejects_empty_path():
    ctrl, _, _ = _setup()
    ok, problems = ctrl.validate("   ")
    assert ok is False and problems


def test_paths_expand_tilde():
    import os
    seen = {}

    def cap(p):
        seen["p"] = p
        return S.StratFile(base_dir=".")

    ctrl, _, _ = _setup(load=cap)
    ctrl.validate("~/run.strat.json")
    assert seen["p"] == os.path.expanduser("~/run.strat.json") and "~" not in seen["p"]
