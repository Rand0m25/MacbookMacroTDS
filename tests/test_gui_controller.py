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
    )
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
