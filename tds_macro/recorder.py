"""Records user play into a StratFile.

Two pieces:
  * :class:`EventCoalescer` — pure, deterministic, fully unit-testable: turns a
    stream of normalized raw events into coalesced strat events (move anchors,
    clicks with double-click merge, drags by a pixel dead-zone — plan S5).
  * :class:`Recorder` — wires the input listeners + window + capture to the
    coalescer, normalizes every event to window-relative coords at capture time,
    drops events fired while Roblox isn't frontmost (R27), and captures
    sync-point reference PNGs on the mark-sync hotkey.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Optional

from .config import Config
from .geometry import Coordinates, Point, Rect, clamp01
from .strat import (
    ClickEvent, DragEvent, Event, Header, KeyPressEvent, KeyReleaseEvent,
    MouseMoveEvent, RecoverySpec, ScrollEvent, StratFile, SyncPointEvent,
)

log = logging.getLogger("tds_macro.recorder")


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


class EventCoalescer:
    """Collapses raw normalized events into strat events. Deterministic + pure."""

    def __init__(self, dead_zone: float = 0.005, double_click_ms: int = 250) -> None:
        self.dead_zone = dead_zone
        self.double_click_ms = double_click_ms
        self.events: list[Event] = []
        self._id = 0
        self._last_move: Optional[tuple[Point, int]] = None  # latest free move (pos, t)
        self._down: dict[str, dict] = {}                      # active button presses
        self._pending_click: Optional[dict] = None            # buffered for dbl-click merge
        # mouse + keyboard listeners and the main (mark-sync) thread all mutate
        # this, so every public method takes the lock (re-entrant for nested calls).
        self._lock = threading.RLock()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _emit(self, ev: Event) -> None:
        self.events.append(ev)

    def _commit_click(self) -> None:
        pc = self._pending_click
        if pc is None:
            return
        self._pending_click = None
        self._emit(ClickEvent(self._next_id(), pc["t"], "click", pos=pc["pos"],
                              button=pc["button"], clicks=pc["clicks"]))

    def _flush_move_anchor(self) -> None:
        """Emit a pending free-move as an anchor (used before keys/scroll/stop)."""
        if self._last_move is None:
            return
        p, t = self._last_move
        self._last_move = None
        self._emit(MouseMoveEvent(self._next_id(), t, "mouse_move", pos=p, duration_ms=0))

    # --- raw event intake (coords already normalized + in-window) ---
    # Every public mutator holds self._lock; private helpers run under it.
    def on_move(self, p: Point, t: int) -> None:
        with self._lock:
            if self._down:
                for info in self._down.values():
                    info["max_dist"] = max(info["max_dist"], _dist(p, info["pos"]))
                    info["last"] = p
            else:
                self._last_move = (p, t)

    def on_button(self, p: Point, button: str, pressed: bool, t: int) -> None:
        with self._lock:
            if pressed:
                # a click/drag carries its own position -> the pre-move is redundant
                self._last_move = None
                self._down[button] = {"pos": p, "t": t, "max_dist": 0.0, "last": p}
                return
            info = self._down.pop(button, None)
            if info is None:
                return
            moved = max(info["max_dist"], _dist(p, info["pos"]))
            if moved > self.dead_zone:
                self._commit_click()
                self._emit(DragEvent(self._next_id(), info["t"], "drag", button=button,
                                     frm=info["pos"], to=p, duration_ms=max(0, t - info["t"])))
            else:
                self._buffer_click(info["pos"], button, info["t"])

    def _buffer_click(self, pos: Point, button: str, t: int) -> None:  # under self._lock
        pc = self._pending_click
        if (pc is not None and pc["button"] == button
                and pc["clicks"] < 2  # only ONE pair coalesces; a 3rd rapid click starts a new event
                and (t - pc["t"]) <= self.double_click_ms  # (else N spam clicks collapse to one double-click)
                and _dist(pos, pc["pos"]) <= self.dead_zone):
            pc["clicks"] = min(2, pc["clicks"] + 1)
            pc["t"] = t
        else:
            self._commit_click()
            self._pending_click = {"pos": pos, "button": button, "t": t, "clicks": 1}

    def on_key(self, key: str, pressed: bool, t: int) -> None:
        with self._lock:
            self._commit_click()
            self._flush_move_anchor()
            cls = KeyPressEvent if pressed else KeyReleaseEvent
            self._emit(cls(self._next_id(), t, "key_press" if pressed else "key_release", key=key))

    def on_scroll(self, p: Optional[Point], dx: int, dy: int, t: int) -> None:
        with self._lock:
            self._commit_click()
            self._flush_move_anchor()
            self._emit(ScrollEvent(self._next_id(), t, "scroll", pos=p, dx=dx, dy=dy))

    def add_sync_point(self, sp: SyncPointEvent) -> None:
        with self._lock:
            self._commit_click()
            self._flush_move_anchor()
            sp.id = self._next_id()
            self._emit(sp)

    def finish(self) -> list[Event]:
        with self._lock:
            self._commit_click()
            self._flush_move_anchor()
            self.events.sort(key=lambda e: (e.t_ms, e.id))
            return self.events


class Recorder:
    """Drives listeners + capture into an :class:`EventCoalescer`."""

    def __init__(self, window, input_backend, capture, config: Config, hotkeys, clock=None) -> None:
        self.window = window
        self.input = input_backend
        self.capture = capture
        self.config = config
        self.hotkeys = hotkeys
        self.clock = clock
        self._geo = None
        self._coords: Optional[Coordinates] = None
        # None (not 0.0) means "epoch unset": a clock can legitimately read 0.0 (FakeClock, or
        # RealClock right after start), and `if not self._t0` would wrongly re-seed it (round 19 #2).
        self._t0: Optional[float] = None
        self._sync_default_region = Rect(0.0, 0.0, 1.0, 1.0)
        # Prefix for auto-generated sync-frame filenames (sync_1.png, ...). The GUI sets a target-specific
        # prefix ("join_sync"/"leave_sync") when recording a join/leave sequence INTO an existing strat, so
        # those captures can't overwrite the main timeline's sync_N.png on disk — the JSON merge keeps the
        # main events pointing at frames/sync_1.png, but a shared filename would clobber its pixels.
        self.sync_label_prefix = "sync"
        dz_px = 6
        self.coalescer = EventCoalescer(double_click_ms=config.double_click_ms)
        self._dz_px = dz_px
        self._sync_count = 0
        self._pressed_keys: set = set()  # keys whose press we recorded (for paired releases)
        # The control hotkeys (panic/start/pause/mark-sync) drive the RECORDING session itself; they
        # must never be captured as game input, or replay re-presses them — and a recorded panic key
        # (default F8) would make the macro stop itself mid-run (observed in a real user recording).
        # Split combos like "ctrl+p" into their individual keys: _on_press fires once per physical key
        # (already normalized to a single name), so we must match each constituent, not the whole string.
        self._hotkey_keys = {
            tok.strip().lower()
            for name in ("panic_hotkey", "start_hotkey", "pause_hotkey", "mark_sync_hotkey")
            for tok in str(getattr(config, name, "")).split("+")
            if tok.strip()
        }
        self._strat_dir = ""  # set in run(); init here so build()/capture_sync_point() are safe early

    def _now_ms(self) -> int:
        base = self._t0 if self._t0 is not None else 0.0  # safe if called before the epoch is seeded
        if self.clock is not None:
            return int(self.clock.now_ms() - base)
        return int((time.monotonic() * 1000) - base)

    def _refresh_geo(self) -> None:
        self._geo = self.window.get_geometry()
        self._coords = Coordinates(self._geo)
        # A minimized/zero-size window would make dz=6.0 (every drag misread as a click);
        # skip the update on degenerate geometry and clamp to a sane max (recheck #w-deadzone).
        if self._geo.w > 0 and self._geo.h > 0:
            self.coalescer.dead_zone = min(0.5, self._dz_px / min(self._geo.w, self._geo.h))

    def _norm(self, x: float, y: float) -> Optional[Point]:
        if not self.window.is_frontmost():
            return None  # R27: ignore events fired at another app
        p = self._coords.logical_to_norm(x, y)
        if self._coords.is_out_of_window(p):
            return None
        return p

    def _recording_paused(self) -> bool:
        # Pause/Resume (GUI button or hotkey) must actually exclude input from the recording,
        # not just toggle a flag the recorder ignores (round 18 #3). New intake is dropped while
        # paused; releases of already-tracked keys/buttons are NEVER gated (below) so nothing
        # gets stuck — mirrors the off-focus (R27) handling.
        return bool(self.hotkeys is not None and self.hotkeys.events.pause.is_set())

    # --- listener callbacks ---
    def _on_move(self, x, y):
        if self._recording_paused() and not self.coalescer._down:
            return  # drop free moves while paused, but keep tracking a drag in progress
        p = self._norm(x, y)
        if p is not None:
            self.coalescer.on_move(p, self._now_ms())
        elif self.coalescer._down and self.window.is_frontmost():
            # a held drag that wandered out-of-window: keep tracking max distance
            # (clamped) so it isn't misclassified as a click on release (recheck #w4)
            self.coalescer.on_move(self._norm_clamped(x, y), self._now_ms())

    def _norm_clamped(self, x, y) -> Point:
        pt = self._coords.logical_to_norm(x, y)
        return Point(clamp01(pt.x), clamp01(pt.y))

    def _on_click(self, x, y, button, pressed):
        if pressed and self._recording_paused():
            return  # drop new presses while paused; releases of tracked buttons still pass below
        p = self._norm(x, y)
        if p is not None:
            self.coalescer.on_button(p, button, pressed, self._now_ms())
        elif not pressed and button in self.coalescer._down:
            # Deliver the RELEASE of a button we're tracking even if it landed
            # out-of-window / off-focus, so the press never gets stuck (D15).
            self.coalescer.on_button(self._norm_clamped(x, y), button, False, self._now_ms())

    def _on_scroll(self, x, y, dx, dy):
        if self._recording_paused():  # excluded from the recording while paused (round 18 #3)
            return
        if not self.window.is_frontmost():  # R27: ignore scrolls aimed at another app (D4 r2)
            return
        p = self._norm(x, y)
        self.coalescer.on_scroll(p, int(dx), int(dy), self._now_ms())

    def _on_press(self, key):
        if self._recording_paused():  # excluded from the recording while paused (round 18 #3)
            return
        if str(key).lower() in self._hotkey_keys:  # F8/F10/… are session controls, not game input
            return
        if not self.window.is_frontmost():  # R27: ignore keys aimed at another app (D14)
            return
        if key in self._pressed_keys:  # ignore OS auto-repeat ticks while a key is held (R6)
            return
        self._pressed_keys.add(key)
        self.coalescer.on_key(key, True, self._now_ms())

    def _on_release(self, key):
        # Always emit the release for a key whose press we recorded (even if focus
        # was lost meanwhile) so the strat never has an unpaired press (D14/D15).
        if key in self._pressed_keys:
            self._pressed_keys.discard(key)
            self.coalescer.on_key(key, False, self._now_ms())

    def capture_sync_point(self, label: str = "", region: Optional[Rect] = None,
                           threshold: Optional[float] = None) -> SyncPointEvent:
        """Grab the current window region and write a reference PNG."""
        from .pngio import frame_to_rgba_bytes, write_png

        if self._geo is None:  # safe if called before run() (recheck #w11)
            self._refresh_geo()
        if self._t0 is None:  # share one epoch with run() so finish()'s t_ms sort stays correct (recheck #w12)
            self._t0 = (self.clock.now_ms() if self.clock else time.monotonic() * 1000)
        region = region or self._sync_default_region
        self._sync_count += 1
        label = label or f"{self.sync_label_prefix}_{self._sync_count}"
        frame = self.capture.grab_region(self._geo, region)
        rel = os.path.join(self.config.frames_dir, f"{label}.png")
        abspath = os.path.join(self._strat_dir, rel)
        data, w, h = frame_to_rgba_bytes(frame)
        write_png(abspath, data, w, h, 4)
        thr = threshold if threshold is not None else self.config.sync_default_threshold  # 0.0 is valid (round 22c #16)
        sp = SyncPointEvent(0, self._now_ms(), "sync_point", label=label, ref_frame=rel,
                            region=region, threshold=thr,
                            timeout_ms=self.config.sync_default_timeout_ms, on_timeout="recover")
        self.coalescer.add_sync_point(sp)
        return sp

    def run(self, strat_path: str, header: Optional[Header] = None, poll_s: float = 0.05) -> StratFile:
        """Block recording until the stop/panic hotkey, then return the StratFile."""
        self._strat_dir = os.path.dirname(os.path.abspath(strat_path))
        if self._t0 is None:  # a pre-run capture_sync_point may already have seeded the shared epoch
            self._t0 = (self.clock.now_ms() if self.clock else time.monotonic() * 1000)  # (#w12 / round 18 #2)
        self._refresh_geo()
        self.input.start_listeners(self._on_move, self._on_click, self._on_scroll,
                                   self._on_press, self._on_release)
        ev = self.hotkeys.events
        try:
            while not ev.is_stop():
                if ev.mark_sync.is_set():
                    ev.mark_sync.clear()
                    try:
                        self.capture_sync_point()
                    except Exception as e:
                        log.warning("mark-sync capture failed (no sync point added): %s", e)
                try:
                    self._refresh_geo()  # a transient window-lookup blip (focus change,
                except Exception:        # brief minimize) must not abort an in-progress recording
                    pass
                time.sleep(poll_s)
        except KeyboardInterrupt:
            # Ctrl-C = stop the recording cleanly and still build/save it, rather than discarding
            # the whole session with an uncaught traceback (round 22c #20)
            log.info("recording interrupted (Ctrl-C); finalizing")
        finally:
            self.input.stop_listeners()
        return self.build(strat_path, header)

    def build(self, strat_path: str, header: Optional[Header] = None) -> StratFile:
        # Drain any keys still physically held when recording stopped. run() calls
        # stop_listeners() first, which stops AND joins the listener threads (round 22b #7) so no
        # callback fires concurrently with this drain; the mock backend also nulls its callbacks.
        # Emit synthetic releases so every recorded press is paired (D14/D15) and replay never
        # leaves a key stuck (round 17 #3).
        for key in list(self._pressed_keys):
            self.coalescer.on_key(key, False, self._now_ms())
        self._pressed_keys.clear()
        # Same for a mouse button still physically held at stop: its release callback was nulled, so
        # without a synthetic release the whole click/drag is silently dropped from the recording
        # (round 17 #3 keyboard fix, mirrored to the mouse path — round 22 #P). info["last"] keeps
        # the accumulated max_dist so drag-vs-click classification stays correct.
        for button, info in list(self.coalescer._down.items()):
            self.coalescer.on_button(info["last"], button, False, self._now_ms())
        events = self.coalescer.finish()
        geo = self._geo or self.window.get_geometry()
        hdr = header or Header()
        if not hdr.window_aspect:
            hdr.window_aspect = round(geo.aspect, 6)
        if not hdr.reference_resolution:
            hdr.reference_resolution = {"w": int(geo.w * geo.retina), "h": int(geo.h * geo.retina)}
        hdr.retina_scale_captured_at = geo.retina
        if not hdr.private_server_url:  # bake the private-server link into the strat (Feature A)
            hdr.private_server_url = getattr(self.config, "private_server_url", "")
        return StratFile(header=hdr, config_overrides={}, events=events,
                         recovery=RecoverySpec(), base_dir=self._strat_dir)
