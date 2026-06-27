"""Control-panel GUI (Tkinter) over the engine/recorder.

Design: ALL behavior lives in the Tk-free :class:`GuiController` (dependency-injected, so
each button's effect is unit-tested with fakes); the Tk widgets in :func:`run_gui` are a thin
view that calls controller methods on click and polls ``controller.status()`` to redraw.
``import tkinter`` happens lazily inside :func:`run_gui`, so importing this module (for tests)
never needs a display.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# default factories (real wiring) — overridable for tests via GuiDeps
# --------------------------------------------------------------------------- #
def _default_build_config(*, overrides=None, loop_count=None, dry_run=None,
                          private_server=None, frames_dir=None):
    import sys

    from .config import Config
    from .cli import _apply_mock

    cfg = Config()
    if overrides:
        cfg = cfg.with_overrides(overrides)
    if loop_count is not None:
        cfg.loop_count = loop_count
    if dry_run is not None:
        cfg.dry_run = dry_run
    if private_server:
        cfg.private_server_url = private_server
    if frames_dir:
        cfg.frames_dir = frames_dir
    if sys.platform != "darwin":  # dev/headless: mock backends (mirrors cli._build_config)
        _apply_mock(cfg)
    return cfg


def _build_config_from(base):
    """A GuiDeps.build_config bound to a given BASE config, so the `gui` CLI command's config
    (--mock / --private-server / --window-rect / overrides) is honored instead of silently dropped
    for a fresh default Config (round 23 #16)."""
    def _bc(*, overrides=None, loop_count=None, dry_run=None, private_server=None, frames_dir=None):
        cfg = base.with_overrides(overrides or {})  # with_overrides returns a fresh copy
        if loop_count is not None:
            cfg.loop_count = loop_count
        if dry_run is not None:
            cfg.dry_run = dry_run
        if private_server:
            cfg.private_server_url = private_server
        if frames_dir:
            cfg.frames_dir = frames_dir
        return cfg
    return _bc


def _default_make_play_engine(st, cfg):
    from .cli import _build_backends
    from .engine import Player
    from .recovery import RecoveryController
    from .hotkeys import HotkeyManager
    from .clock import RealClock

    window, inp, cap, cmp = _build_backends(cfg)
    hk = HotkeyManager(cfg)
    clock = RealClock(should_abort=hk.should_abort)
    recovery = RecoveryController(st, window, inp, cap, cmp, clock, cfg)
    return Player(st, window, inp, cap, cmp, clock, recovery, cfg, hotkeys=hk), hk


def _default_make_record_engine(cfg):
    from .cli import _build_backends
    from .recorder import Recorder
    from .hotkeys import HotkeyManager
    from .clock import RealClock

    window, inp, cap, _cmp = _build_backends(cfg)
    hk = HotkeyManager(cfg)
    clock = RealClock(should_abort=hk.should_abort)
    return Recorder(window, inp, cap, cfg, hk, clock=clock), hk


def _default_load_strat(path):
    from .strat import load

    return load(path)


def _default_save_strat(strat, path):
    from .strat import save

    save(strat, path)


def _default_consent_ok():
    import os

    from .cli import CONSENT_PATH

    return os.path.exists(CONSENT_PATH)


def _default_set_consent():
    from datetime import datetime, timezone

    from .cli import CONSENT_PATH

    try:
        with open(CONSENT_PATH, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except OSError:
        pass


@dataclass
class GuiDeps:
    build_config: Callable = _default_build_config
    make_play_engine: Callable = _default_make_play_engine
    make_record_engine: Callable = _default_make_record_engine
    load_strat: Callable = _default_load_strat
    save_strat: Callable = _default_save_strat
    consent_ok: Callable = _default_consent_ok
    set_consent: Callable = _default_set_consent
    make_header: Optional[Callable] = None  # (name, map, difficulty) -> Header


# --------------------------------------------------------------------------- #
# controller (pure logic, no tkinter)
# --------------------------------------------------------------------------- #
class GuiController:
    """Every button/action maps to a method here; the Tk view only calls these."""

    def __init__(self, deps: Optional[GuiDeps] = None, on_event: Optional[Callable] = None):
        self.deps = deps or GuiDeps()
        self.on_event = on_event or (lambda kind, payload=None: None)
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._hk = None
        self._player = None
        self._activity = "idle"  # idle | record | play

    # -- helpers --
    def _emit(self, kind: str, payload=None) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def is_busy(self) -> bool:
        with self._lock:
            return self._activity != "idle"

    def _spawn(self, hk, activity_label: str) -> bool:
        """Start hotkeys + the already-assigned worker thread. If thread.start() fails (e.g. a
        RuntimeError under thread exhaustion), reset to idle so the controller isn't left
        permanently 'busy' with a thread whose finally never ran (round 22b #10)."""
        self._safe_start_hotkeys(hk)
        try:
            self._thread.start()
        except Exception as e:
            try:
                hk.stop()
            except Exception:
                pass
            with self._lock:
                self._activity, self._player, self._hk, self._thread = "idle", None, None, None
            self._emit("error", f"could not start: {e}")
            return False
        self._emit("state", activity_label)
        return True

    # -- Validate button --
    def validate(self, path: str, private_server: str = ""):
        from .errors import StratValidationError

        try:
            st = self.deps.load_strat(path)
        except StratValidationError as e:
            return False, list(e.problems)
        except (OSError, ValueError) as e:
            # ValueError covers UnicodeDecodeError when the file isn't UTF-8/JSON (a binary
            # file): keep the (ok, problems) contract instead of crashing the Tk callback
            # with a traceback (round 18 #4).
            return False, [str(e)]
        # Exercise the SAME config build Play uses — overrides coercion AND the typed private-server
        # link — so Validate and Play agree (a bad link no longer gets a false green light) (round
        # 22c #18 + round 23 #10).
        try:
            cfg = self.deps.build_config(overrides=getattr(st, "config_overrides", None),
                                         private_server=private_server)
        except Exception as e:
            return False, [f"bad config_overrides: {e}"]
        problems = cfg.validate()
        if problems:
            return False, problems
        return True, []

    # -- Play button --
    def start_play(self, path: str, *, loop_count=0, dry_run=False,
                   private_server="", accept_ban_risk=False) -> bool:
        with self._lock:
            if self._activity != "idle":
                self._emit("error", "already running")
                return False
            if accept_ban_risk:
                self.deps.set_consent()  # best-effort persist; honored in-memory below even if the write fails
            # An explicit accept this session counts even if set_consent() couldn't write
            # to disk (read-only home / full disk) -> don't dead-end the user by asking them
            # to tick a box they just ticked (round 17 #4).
            if not (accept_ban_risk or self.deps.consent_ok()):
                self._emit("consent_required")
                return False
            try:
                st = self.deps.load_strat(path)
            except Exception as e:
                self._emit("error", f"load failed: {e}")
                return False
            try:
                cfg = self.deps.build_config(overrides=getattr(st, "config_overrides", None),
                                             loop_count=loop_count, dry_run=dry_run,
                                             private_server=private_server)
            except Exception as e:
                self._emit("error", f"could not start: {e}")
                return False
            problems = cfg.validate()  # enforce the CLI's gates (URL host, window_title_match…) (round 22 #I)
            if problems:
                self._emit("error", "invalid config: " + "; ".join(problems))
                return False
            try:
                player, hk = self.deps.make_play_engine(st, cfg)
            except Exception as e:
                self._emit("error", f"could not start: {e}")
                return False
            self._player, self._hk, self._activity = player, hk, "play"
            self._thread = threading.Thread(target=self._run, args=("play", player.run, hk),
                                            daemon=True)
        return self._spawn(hk, "play")

    # -- Record button --
    def start_record(self, path: str, *, name="", map="", difficulty="", private_server="") -> bool:
        with self._lock:
            if self._activity != "idle":
                self._emit("error", "already running")
                return False
            try:
                cfg = self.deps.build_config(private_server=private_server)
            except Exception as e:
                self._emit("error", f"could not start: {e}")
                return False
            problems = cfg.validate()  # validate before recording too (round 22 #I)
            if problems:
                self._emit("error", "invalid config: " + "; ".join(problems))
                return False
            try:
                recorder, hk = self.deps.make_record_engine(cfg)
            except Exception as e:
                self._emit("error", f"could not start: {e}")
                return False
            header = self._make_header(name, map, difficulty)
            save_strat = self.deps.save_strat

            def _do():
                strat = recorder.run(path, header=header)
                try:
                    save_strat(strat, path)
                    self._emit("log", f"saved {len(strat.events)} events to {path}")
                except Exception as e:
                    # don't lose a just-recorded session if the typed path is unwritable: fall back
                    # to a unique temp file and tell the user where it is (round 22 #S).
                    import os
                    import tempfile
                    fd, fallback = tempfile.mkstemp(prefix="tds_recording_", suffix=".strat.json")
                    os.close(fd)
                    try:
                        save_strat(strat, fallback)
                        self._emit("error", f"could not save to {path} ({e}); saved a copy to {fallback}")
                    except Exception as e2:
                        self._emit("error", f"could not save the recording ({e}); fallback also failed ({e2})")

            self._player, self._hk, self._activity = None, hk, "record"
            self._thread = threading.Thread(target=self._run, args=("record", _do, hk), daemon=True)
        return self._spawn(hk, "record")

    # -- Pause/Resume button --
    def pause_toggle(self) -> bool:
        with self._lock:
            hk = self._hk
        if hk is None:
            return False
        return hk.events.toggle_pause()  # atomic; can't race the pause hotkey (round 22 #R)

    # -- Stop / Panic button (and window close) --
    def stop(self) -> bool:
        with self._lock:
            hk, t = self._hk, self._thread
        if hk is not None:
            hk.events.panic.set()
            hk.events.stop.set()
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)
        return True

    # -- status (polled by the view) --
    def status(self) -> dict:
        with self._lock:
            act, player = self._activity, self._player
        d = {"busy": act != "idle", "activity": act, "state": "-",
             "runs": 0, "wins": 0, "losses": 0, "recoveries": 0, "sync_timeouts": 0}
        if player is not None:
            d["state"] = getattr(player.state, "value", str(player.state))
            s = player.stats
            d.update(runs=s.runs, wins=s.wins, losses=s.losses,
                     recoveries=s.recoveries, sync_timeouts=s.sync_timeouts)
        elif act == "record":
            d["state"] = "recording"
        return d

    # -- internals --
    def _make_header(self, name, map, difficulty):
        if self.deps.make_header is not None:
            return self.deps.make_header(name, map, difficulty)
        import os
        from datetime import datetime, timezone

        from .strat import Header

        return Header(name=name, map=map, difficulty=difficulty,
                      created=datetime.now(timezone.utc).isoformat(),
                      created_by=os.environ.get("USER", ""))

    def _safe_start_hotkeys(self, hk) -> None:
        try:
            hk.start()
        except Exception:
            pass

    def _run(self, activity, fn, hk) -> None:
        try:
            fn()
        except Exception as e:
            self._emit("error", f"{activity} crashed: {e}")
        finally:
            try:
                hk.stop()
            except Exception:
                pass
            with self._lock:
                self._activity, self._player, self._hk = "idle", None, None
            self._emit("done", activity)


# --------------------------------------------------------------------------- #
# Tk view (thin) — lazily imports tkinter
# --------------------------------------------------------------------------- #
def run_gui(config=None) -> int:
    try:
        # macOS ships a deprecated system Tk that prints a DEPRECATION WARNING the first time
        # Tk() initializes; silence it (set before tkinter loads). setdefault respects a user
        # who deliberately set it to 0. Harmless no-op off macOS.
        os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
        import tkinter as tk
        from tkinter import filedialog, ttk
        root = tk.Tk()  # also fails here on a headless box (no $DISPLAY)
    except Exception as e:  # no Tk / headless
        print(f"GUI unavailable ({type(e).__name__}: {e}). Use the CLI instead.")
        return 1

    # Build pynput's macOS keyboard context on THIS (main) thread before any Record/Play
    # starts a listener thread, or macOS 15 SIGSEGVs the process (pynput #511/#512).
    from .input_backend import prewarm_macos_keyboard
    prewarm_macos_keyboard()

    root.title("TDS Macro")
    # Honor the config the `gui` CLI command built (--mock, --private-server, --window-rect, …);
    # without this it was silently ignored and a fresh default Config used (round 23 #16).
    deps = GuiDeps(build_config=_build_config_from(config)) if config is not None else None
    ctrl = GuiController(deps)
    pad = {"padx": 6, "pady": 3}

    def log(msg):
        logbox.configure(state="normal")
        logbox.insert("end", str(msg) + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def on_event(kind, payload=None):
        if kind == "consent_required":
            root.after(0, lambda: log("Ban-risk acknowledgement required — tick 'Accept ban risk' to play."))
        elif kind in ("error", "log"):
            root.after(0, lambda: log(f"{kind}: {payload}" if kind == "error" else payload))
        elif kind == "done":
            root.after(0, lambda: log(f"{payload} finished"))
    ctrl.on_event = on_event

    # Tk 9.0 on macOS renders a ttk.Frame gridded with sticky="nsew" but no parent grid
    # weights as a ZERO-size (blank) window — even a full-window resize won't reveal the
    # widgets. Packing the frame (the path verified to render on the user's Mac) fills the
    # toplevel reliably; the children inside still use grid (grid-in-frame is allowed since
    # frm is a separate container from root).
    frm = ttk.Frame(root)
    frm.pack(fill="both", expand=True)

    # strat file row
    ttk.Label(frm, text="Strat file").grid(row=0, column=0, sticky="w", **pad)
    path_var = tk.StringVar()
    ttk.Entry(frm, textvariable=path_var, width=44).grid(row=0, column=1, columnspan=2, **pad)
    ttk.Button(frm, text="Browse…",
               command=lambda: path_var.set(filedialog.askopenfilename() or path_var.get())
               ).grid(row=0, column=3, **pad)

    def do_validate():
        ok, problems = ctrl.validate(path_var.get(), private_server=link_var.get())  # vet the link too (round 23 #10)
        log("valid ✓" if ok else "invalid ✗")
        for p in problems:
            log("  - " + p)
    ttk.Button(frm, text="Validate", command=do_validate).grid(row=0, column=4, **pad)

    # metadata + link
    name_var, map_var, diff_var, link_var = (tk.StringVar() for _ in range(4))
    for i, (lbl, var) in enumerate([("Name", name_var), ("Map", map_var), ("Difficulty", diff_var)]):
        ttk.Label(frm, text=lbl).grid(row=1, column=i * 2, sticky="e", **pad)
        ttk.Entry(frm, textvariable=var, width=14).grid(row=1, column=i * 2 + 1, **pad)
    ttk.Label(frm, text="Private server link").grid(row=2, column=0, sticky="e", **pad)
    ttk.Entry(frm, textvariable=link_var, width=44).grid(row=2, column=1, columnspan=3, **pad)

    # options
    loop_var = tk.IntVar(value=0)
    dry_var = tk.BooleanVar(value=False)
    ban_var = tk.BooleanVar(value=False)
    ttk.Label(frm, text="Loop count (0=∞)").grid(row=3, column=0, sticky="e", **pad)
    ttk.Spinbox(frm, from_=0, to=100000, textvariable=loop_var, width=8).grid(row=3, column=1, sticky="w", **pad)
    ttk.Checkbutton(frm, text="Dry run", variable=dry_var).grid(row=3, column=2, **pad)
    ttk.Checkbutton(frm, text="Accept ban risk", variable=ban_var).grid(row=3, column=3, **pad)

    # actions
    def do_record():
        if ctrl.start_record(path_var.get(), name=name_var.get(), map=map_var.get(),
                             difficulty=diff_var.get(), private_server=link_var.get()):
            log("recording… press Stop (or F8) to finish")

    def do_play():
        try:
            loops = loop_var.get()
        except tk.TclError:  # spinbox blanked / non-numeric -> treat as infinite
            loops = 0
        if ctrl.start_play(path_var.get(), loop_count=loops, dry_run=dry_var.get(),
                          private_server=link_var.get(), accept_ban_risk=ban_var.get()):
            log("playing…")

    ttk.Button(frm, text="Record", command=do_record).grid(row=4, column=0, **pad)
    ttk.Button(frm, text="Play", command=do_play).grid(row=4, column=1, **pad)
    ttk.Button(frm, text="Pause/Resume", command=ctrl.pause_toggle).grid(row=4, column=2, **pad)
    ttk.Button(frm, text="Stop / Panic", command=ctrl.stop).grid(row=4, column=3, **pad)

    status_var = tk.StringVar(value="idle")
    ttk.Label(frm, textvariable=status_var).grid(row=5, column=0, columnspan=5, sticky="w", **pad)

    logbox = tk.Text(frm, height=12, width=70, state="disabled")
    logbox.grid(row=6, column=0, columnspan=5, **pad)

    def poll():
        s = ctrl.status()
        status_var.set(f"{s['activity']}  state={s['state']}  runs={s['runs']}  "
                       f"W{s['wins']}/L{s['losses']}  rec={s['recoveries']}  "
                       f"sync_timeouts={s['sync_timeouts']}")
        root.after(200, poll)
    poll()

    closing = False

    def on_close():
        # root.update() below re-pumps the Tk event loop, which can deliver a second
        # WM_DELETE_WINDOW (or otherwise tear the app down) and re-enter on_close — so the
        # teardown must run exactly once, or the second root.destroy() hits an already-
        # destroyed application ("can't invoke destroy command", gui_debug.py).
        nonlocal closing
        if closing:
            return
        closing = True
        ctrl.stop()  # joins the worker, so a record session is saved before we tear down
        try:
            root.update()  # flush the "saved N events"/"done" after-callbacks so they aren't lost (round 22c #20)
        except Exception:
            pass
        try:
            root.destroy()
        except tk.TclError:
            pass  # already torn down by a re-entrant close while update() pumped the loop
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.update_idletasks()  # force the initial layout/paint (some macOS/Tk 9 builds show blank otherwise)
    root.mainloop()
    return 0
