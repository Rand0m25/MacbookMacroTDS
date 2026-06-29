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


def _default_load_strat_lenient(path):
    # Used only to MERGE a recorded join/leave sequence into an existing strat: we just need its
    # structure, so skip reference-frame existence checks (a missing frame must not block the merge).
    from .strat import load

    return load(path, check_frames=False)


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


def _default_load_settings():
    from . import settings

    return settings.load()


def _default_save_settings(values):
    from . import settings

    settings.save(values)


def _default_validate_settings(values):
    from . import settings

    return settings.validate(values)


def _default_settings_defaults():
    from . import settings

    return settings.defaults()


@dataclass
class GuiDeps:
    build_config: Callable = _default_build_config
    make_play_engine: Callable = _default_make_play_engine
    make_record_engine: Callable = _default_make_record_engine
    load_strat: Callable = _default_load_strat
    load_strat_lenient: Callable = _default_load_strat_lenient  # frame-check-free load for join/leave merge
    save_strat: Callable = _default_save_strat
    consent_ok: Callable = _default_consent_ok
    set_consent: Callable = _default_set_consent
    make_header: Optional[Callable] = None  # (name, map, difficulty) -> Header
    # persisted GUI settings (a curated subset of Config applied as overrides) — see tds_macro/settings.py
    load_settings: Callable = _default_load_settings
    save_settings: Callable = _default_save_settings
    validate_settings: Callable = _default_validate_settings
    settings_defaults: Callable = _default_settings_defaults


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
        # effective (full) settings = defaults with the user's saved overrides layered on top; a corrupt
        # or absent file degrades to defaults (settings.load returns {}).
        try:
            self.settings = {**self.deps.settings_defaults(), **(self.deps.load_settings() or {})}
        except Exception:
            self.settings = dict(self.deps.settings_defaults())

    # -- settings (a curated subset of Config, persisted, applied as config overrides) --
    def effective_settings(self) -> dict:
        """The current value of every editable setting (for the Settings window to populate)."""
        with self._lock:
            return dict(self.settings)

    def _merged_overrides(self, strat_overrides=None) -> dict:
        """Config overrides for a run: user settings as the base, the strat's own config_overrides on
        top (a strat stays the most-specific source, matching prior precedence)."""
        with self._lock:
            base = dict(self.settings)
        base.update(strat_overrides or {})
        return base

    def save_settings(self, values: dict):
        """Validate, persist (best-effort), and apply new settings. Idle-only. Returns (ok, problems)."""
        if self.is_busy():
            return False, ["stop the current activity before changing settings"]
        problems = self.deps.validate_settings(values)
        if problems:
            return False, list(problems)
        merged = {**self.effective_settings(), **values}
        try:
            self.deps.save_settings(merged)  # persist; honored in-memory below even if the write fails
        except Exception as e:
            self._emit("error", f"settings applied but could not be saved: {e}")
        with self._lock:
            self.settings = merged
        self._emit("log", "settings saved")
        return True, []

    def reset_settings(self):
        """Restore (and persist) the default settings. Idle-only. Returns (ok, problems)."""
        if self.is_busy():
            return False, ["stop the current activity before changing settings"]
        defaults = dict(self.deps.settings_defaults())
        try:
            self.deps.save_settings(defaults)
        except Exception as e:
            self._emit("error", f"settings reset but could not be saved: {e}")
        with self._lock:
            self.settings = defaults
        self._emit("log", "settings reset to defaults")
        return True, []

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

        path = os.path.expanduser((path or "").strip())
        if not path:
            return False, ["choose a strat file first"]
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
            cfg = self.deps.build_config(overrides=self._merged_overrides(getattr(st, "config_overrides", None)),
                                         private_server=private_server)
        except Exception as e:
            return False, [f"bad config_overrides: {e}"]
        problems = cfg.validate()
        if problems:
            return False, problems
        return True, []

    # -- New… button (create a blank strat file instead of choosing one) --
    def new_strat(self, path: str, *, name="", map="", difficulty="") -> bool:
        """Create a fresh, well-formed (empty) strat file at PATH so the user can record into /
        edit it, instead of only being able to pick an existing one. The metadata fields are baked
        into the header. Overwrites freely — the Tk view's asksaveasfilename already confirms
        overwrite, and "New" intentionally replaces whatever is there."""
        from .strat import StratFile

        path = os.path.expanduser((path or "").strip())  # a Tk entry doesn't expand ~; reject empty up front
        if not path:
            self._emit("error", "choose a name/location for the new strat file first "
                                "(e.g. ~/tds_run.strat.json)")
            return False
        with self._lock:
            if self._activity != "idle":  # don't clobber the file a record/play is mid-using
                self._emit("error", "stop the current activity before creating a new file")
                return False
        header = self._make_header(name, map, difficulty)
        try:
            self.deps.save_strat(StratFile(header=header), path)
        except Exception as e:
            self._emit("error", f"could not create {path}: {e}")
            return False
        self._emit("log", f"created new strat file {path}")
        return True

    # -- Play button --
    def start_play(self, path: str, *, loop_count=0, dry_run=False,
                   private_server="", accept_ban_risk=False) -> bool:
        path = os.path.expanduser((path or "").strip())  # expand ~; an empty path is a clear error
        if not path:
            self._emit("error", "choose a strat file to play first")
            return False
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
                cfg = self.deps.build_config(overrides=self._merged_overrides(getattr(st, "config_overrides", None)),
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

    # which StratFile field each record target writes to (and a human label for messages)
    _TARGET_FIELD = {"events": "events", "join": "join_sequence", "leave": "leave_reset_sequence"}

    def _save_recording_fallback(self, strat, reason: str) -> None:
        """Last resort so a just-recorded session is never lost: write it to a unique temp file (never
        the user's chosen path) and surface why. Used when the target file can't be merged-into or
        written (round 22 #S / review round 24 #A)."""
        import os
        import tempfile

        fd, fallback = tempfile.mkstemp(prefix="tds_recording_", suffix=".strat.json")
        os.close(fd)
        try:
            self.deps.save_strat(strat, fallback)
            self._emit("error", f"{reason}; saved the recording to {fallback}")
        except Exception as e2:
            self._emit("error", f"{reason}; and the fallback save also failed ({e2})")

    def _merge_recorded(self, recorded, path: str, target: str):
        """Decide the StratFile to save for a recording. For ``events`` it's the recording as-is (the
        old behavior). For ``join``/``leave`` we load the EXISTING strat so its other fields survive and
        replace ONLY that sequence with the recorded events — so you can add a join/leave sequence to a
        strat without clobbering its main timeline. ``leave_reset_sequence`` is replayed on a fixed timer
        and the validator rejects sync_points there, so those are stripped (with a log).

        A genuinely-ABSENT file (FileNotFoundError) starts fresh. But a file that EXISTS yet won't parse
        must NOT be silently overwritten — that would destroy the user's strat — so we re-raise and let
        the caller save the raw recording to a fallback file instead (review round 24 #A)."""
        if target == "events":
            return recorded
        events = list(recorded.events)
        if target == "leave":
            kept = [e for e in events if getattr(e, "type", "") != "sync_point"]
            if len(kept) != len(events):
                self._emit("log", f"stripped {len(events) - len(kept)} sync point(s): "
                                  "leave_reset_sequence is replayed on a timer, not the visual engine")
            events = kept
        try:
            base = self.deps.load_strat_lenient(path)  # preserve header/events/recovery of the existing strat
        except FileNotFoundError:
            base = recorded            # no file yet -> the recording becomes a new strat, but its events
            base.events = []           # belong in the target sequence, not the main timeline
        # any OTHER load error (StratValidationError / OSError / JSON) propagates -> caller won't overwrite
        setattr(base, self._TARGET_FIELD[target], events)
        return base

    # -- Record button --
    def start_record(self, path: str, *, name="", map="", difficulty="", private_server="",
                     target="events") -> bool:
        # Expand ~ (a Tk entry doesn't) and reject an empty path up front: otherwise the save at the
        # end of recording fails deep in os.replace with a cryptic "No such file or directory".
        path = os.path.expanduser((path or "").strip())
        if not path:
            self._emit("error", "choose a file to save the recording to first (e.g. ~/tds_run.strat.json)")
            return False
        if target not in self._TARGET_FIELD:
            self._emit("error", f"unknown record target {target!r}")
            return False
        with self._lock:
            if self._activity != "idle":
                self._emit("error", "already running")
                return False
            try:
                cfg = self.deps.build_config(overrides=self._merged_overrides(), private_server=private_server)
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
            # Namespace captured reference frames by record target so recording a join/leave sequence
            # into an EXISTING strat can't overwrite the main timeline's sync_N.png on disk (the JSON
            # merge keeps the main events pointing at frames/sync_1.png; a shared filename would clobber
            # its pixels and make that sync compare against the wrong image forever).
            try:
                recorder.sync_label_prefix = "sync" if target == "events" else f"{target}_sync"
            except (AttributeError, TypeError):
                pass  # an injected recorder that doesn't support it just keeps its own naming
            header = self._make_header(name, map, difficulty)
            save_strat = self.deps.save_strat

            def _do():
                recorded = recorder.run(path, header=header)
                field = self._TARGET_FIELD[target]
                try:
                    out = self._merge_recorded(recorded, path, target)
                except Exception as e:
                    # the file EXISTS but couldn't be parsed to merge into. Do NOT overwrite it (that
                    # would destroy the user's strat) — save the raw recording to a fallback file and
                    # tell them their original is untouched (review round 24 #A).
                    self._save_recording_fallback(
                        recorded, f"could not read {path} to merge into {field} ({e}); it was left untouched")
                    return
                try:
                    save_strat(out, path)
                    self._emit("log", f"saved {len(getattr(out, field) or [])} events to {field} of {path}")
                except Exception as e:
                    # don't lose a just-recorded session if the typed path is unwritable (round 22 #S)
                    self._save_recording_fallback(out, f"could not save to {path} ({e})")

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

    def _surface_failed_play(self, activity: str) -> None:
        """A play that completed ZERO matches and failed (cold-start launch whose window never appeared,
        recovery giving up, restart budget exhausted) is swallowed by Player.run() into a graceful
        stopped_reason and returns normally — so without this the GUI would only log 'play finished' with
        no hint of the failure (review round 24 #2/#B). Emit an explicit error in that case. Shares
        RunStats.is_failure() with the CLI exit code so the two agree."""
        with self._lock:
            player = self._player
        if activity != "play" or player is None:
            return
        stats = getattr(player, "stats", None)
        if stats is not None and stats.is_failure():
            self._emit("error", f"play failed: {stats.stopped_reason or 'no matches completed'}")

    def _run(self, activity, fn, hk) -> None:
        try:
            fn()
            self._surface_failed_play(activity)  # turn a silent zero-match failure into an error event
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

    # metadata + link vars (defined before the strat-file row so New… can read them into the header)
    name_var, map_var, diff_var, link_var = (tk.StringVar() for _ in range(4))

    # strat file row
    ttk.Label(frm, text="Strat file").grid(row=0, column=0, sticky="w", **pad)
    path_var = tk.StringVar()
    ttk.Entry(frm, textvariable=path_var, width=44).grid(row=0, column=1, columnspan=2, **pad)
    # Browse… picks an EXISTING file (open dialog); New… names a not-yet-existing one (save dialog)
    # and creates a blank strat there, so you don't have to hand-type a path to record into.
    ttk.Button(frm, text="Browse…",
               command=lambda: path_var.set(filedialog.askopenfilename() or path_var.get())
               ).grid(row=0, column=3, **pad)

    def do_new():
        chosen = filedialog.asksaveasfilename(
            title="Create new strat file", defaultextension=".strat.json",
            initialfile="tds_run.strat.json",
            filetypes=[("Strat files", "*.strat.json"), ("JSON", "*.json"), ("All files", "*.*")])
        if not chosen:  # user cancelled — leave the current path untouched
            return
        if ctrl.new_strat(chosen, name=name_var.get(), map=map_var.get(), difficulty=diff_var.get()):
            path_var.set(chosen)  # only adopt the path once the file actually got created
    ttk.Button(frm, text="New…", command=do_new).grid(row=0, column=4, **pad)

    def do_validate():
        ok, problems = ctrl.validate(path_var.get(), private_server=link_var.get())  # vet the link too (round 23 #10)
        log("valid ✓" if ok else "invalid ✗")
        for p in problems:
            log("  - " + p)
    ttk.Button(frm, text="Validate", command=do_validate).grid(row=0, column=5, **pad)

    # metadata + link
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

    # which sequence Record captures into. Main timeline = the in-match events (default); the other two
    # let you record the lobby rejoin (join_sequence) and the Roblox leave/reset path (leave_reset_sequence)
    # straight into the same strat file instead of hand-merging JSON.
    _TARGET_LABELS = {"Main timeline": "events", "Join sequence": "join", "Leave/reset sequence": "leave"}
    target_var = tk.StringVar(value="Main timeline")
    ttk.Label(frm, text="Record into").grid(row=3, column=4, sticky="e", **pad)
    ttk.Combobox(frm, textvariable=target_var, state="readonly", width=18,
                 values=list(_TARGET_LABELS)).grid(row=3, column=5, **pad)

    # actions
    def do_record():
        target = _TARGET_LABELS.get(target_var.get(), "events")
        if ctrl.start_record(path_var.get(), name=name_var.get(), map=map_var.get(),
                             difficulty=diff_var.get(), private_server=link_var.get(), target=target):
            into = "main timeline" if target == "events" else f"{target} sequence"
            log(f"recording into {into}… press Stop (or F8) to finish"
                + ("  (don't drop sync points — F10 — into the leave sequence)" if target == "leave" else ""))

    def do_play():
        try:
            loops = loop_var.get()
        except tk.TclError:  # spinbox blanked / non-numeric -> treat as infinite
            loops = 0
        if ctrl.start_play(path_var.get(), loop_count=loops, dry_run=dry_var.get(),
                          private_server=link_var.get(), accept_ban_risk=ban_var.get()):
            log("playing…")

    def open_settings():
        from . import settings as _settings
        if ctrl.is_busy():  # settings are edit-while-idle only
            log("settings: stop the current activity first")
            return
        win = tk.Toplevel(root)
        win.title("Settings")
        win.transient(root)
        cur = ctrl.effective_settings()
        vars_by_field = {}

        def _repopulate(values):
            for fld, (var, knd) in vars_by_field.items():
                if knd == "bool":
                    var.set(bool(values.get(fld, False)))
                else:
                    var.set("" if values.get(fld) is None else str(values.get(fld)))

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=8, pady=6)
        left, right = ttk.Frame(body), ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nw", padx=6)
        right.grid(row=0, column=1, sticky="nw", padx=6)
        for gtitle, items in _settings.GROUPS:
            # the big Recovery/safety group gets its own column; the rest stack on the left
            lf = ttk.LabelFrame(right if gtitle.startswith("Recovery") else left, text=gtitle)
            lf.pack(fill="x", pady=4, anchor="n")
            for ri, (field, kind, label) in enumerate(items):
                ttk.Label(lf, text=label).grid(row=ri, column=0, sticky="w", padx=4, pady=2)
                if kind == "bool":
                    var = tk.BooleanVar(value=bool(cur.get(field, False)))
                    ttk.Checkbutton(lf, variable=var).grid(row=ri, column=1, sticky="w", padx=4)
                else:
                    val = cur.get(field)
                    var = tk.StringVar(value="" if val is None else str(val))
                    ttk.Entry(lf, textvariable=var, width=12).grid(row=ri, column=1, sticky="w", padx=4)
                vars_by_field[field] = (var, kind)

        msg = tk.Label(win, text="", fg="red", wraplength=520, justify="left")
        msg.pack(fill="x", padx=10)

        def _collect():
            out = {}
            for field, (var, kind) in vars_by_field.items():
                v = var.get()
                if kind in ("int", "float") and isinstance(v, str) and not v.strip():
                    continue  # a blank numeric field -> keep the current value, don't send ""
                out[field] = v
            return out

        def do_save():
            ok, problems = ctrl.save_settings(_collect())
            if ok:
                win.destroy()
            else:
                msg.config(text="  ·  ".join(problems))

        def do_reset():
            ok, problems = ctrl.reset_settings()
            if ok:
                _repopulate(ctrl.effective_settings())
                msg.config(text="reset to defaults (Save not needed — already applied)", fg="green")
            else:
                msg.config(text="  ·  ".join(problems), fg="red")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Save", command=do_save).pack(side="left")
        ttk.Button(btns, text="Reset to defaults", command=do_reset).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        win.update_idletasks()

    ttk.Button(frm, text="Record", command=do_record).grid(row=4, column=0, **pad)
    ttk.Button(frm, text="Play", command=do_play).grid(row=4, column=1, **pad)
    ttk.Button(frm, text="Pause/Resume", command=ctrl.pause_toggle).grid(row=4, column=2, **pad)
    ttk.Button(frm, text="Stop / Panic", command=ctrl.stop).grid(row=4, column=3, **pad)
    ttk.Button(frm, text="Settings…", command=open_settings).grid(row=4, column=4, **pad)

    status_var = tk.StringVar(value="idle")
    ttk.Label(frm, textvariable=status_var).grid(row=5, column=0, columnspan=6, sticky="w", **pad)

    logbox = tk.Text(frm, height=12, width=70, state="disabled")
    logbox.grid(row=6, column=0, columnspan=6, **pad)

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
