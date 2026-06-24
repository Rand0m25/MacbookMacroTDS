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

import math
import os
import threading
import time
from typing import Optional

from .config import Config
from .geometry import Coordinates, Point, Rect, clamp01
from .strat import (
    ClickEvent, DragEvent, Event, Header, KeyPressEvent, KeyReleaseEvent,
    MouseMoveEvent, RecoverySpec, ScrollEvent, StratFile, SyncPointEvent, WaitEvent,
)


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
                and (t - pc["t"]) <= self.double_click_ms
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
        self._t0 = 0.0
        self._sync_default_region = Rect(0.0, 0.0, 1.0, 1.0)
        dz_px = 6
        self.coalescer = EventCoalescer(double_click_ms=config.double_click_ms)
        self._dz_px = dz_px
        self._sync_count = 0
        self._pressed_keys: set = set()  # keys whose press we recorded (for paired releases)

    def _now_ms(self) -> int:
        if self.clock is not None:
            return int(self.clock.now_ms() - self._t0)
        return int((time.monotonic() * 1000) - self._t0)

    def _refresh_geo(self) -> None:
        self._geo = self.window.get_geometry()
        self._coords = Coordinates(self._geo)
        dz = self._dz_px / max(1, min(self._geo.w, self._geo.h))
        self.coalescer.dead_zone = dz

    def _norm(self, x: float, y: float) -> Optional[Point]:
        if not self.window.is_frontmost():
            return None  # R27: ignore events fired at another app
        p = self._coords.logical_to_norm(x, y)
        if self._coords.is_out_of_window(p):
            return None
        return p

    # --- listener callbacks ---
    def _on_move(self, x, y):
        p = self._norm(x, y)
        if p:
            self.coalescer.on_move(p, self._now_ms())

    def _norm_clamped(self, x, y) -> Point:
        pt = self._coords.logical_to_norm(x, y)
        return Point(clamp01(pt.x), clamp01(pt.y))

    def _on_click(self, x, y, button, pressed):
        p = self._norm(x, y)
        if p is not None:
            self.coalescer.on_button(p, button, pressed, self._now_ms())
        elif not pressed and button in self.coalescer._down:
            # Deliver the RELEASE of a button we're tracking even if it landed
            # out-of-window / off-focus, so the press never gets stuck (D15).
            self.coalescer.on_button(self._norm_clamped(x, y), button, False, self._now_ms())

    def _on_scroll(self, x, y, dx, dy):
        if not self.window.is_frontmost():  # R27: ignore scrolls aimed at another app (D4 r2)
            return
        p = self._norm(x, y)
        self.coalescer.on_scroll(p, int(dx), int(dy), self._now_ms())

    def _on_press(self, key):
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

        region = region or self._sync_default_region
        self._sync_count += 1
        label = label or f"sync_{self._sync_count}"
        frame = self.capture.grab_region(self._geo, region)
        rel = os.path.join(self.config.frames_dir, f"{label}.png")
        abspath = os.path.join(self._strat_dir, rel)
        data, w, h = frame_to_rgba_bytes(frame)
        write_png(abspath, data, w, h, 4)
        sp = SyncPointEvent(0, self._now_ms(), "sync_point", label=label, ref_frame=rel,
                            region=region, threshold=threshold or self.config.sync_default_threshold,
                            timeout_ms=self.config.sync_default_timeout_ms, on_timeout="recover")
        self.coalescer.add_sync_point(sp)
        return sp

    def run(self, strat_path: str, header: Optional[Header] = None, poll_s: float = 0.05) -> StratFile:
        """Block recording until the stop/panic hotkey, then return the StratFile."""
        self._strat_dir = os.path.dirname(os.path.abspath(strat_path))
        self._t0 = (self.clock.now_ms() if self.clock else time.monotonic() * 1000)
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
                    except Exception:
                        pass
                self._refresh_geo()
                time.sleep(poll_s)
        finally:
            self.input.stop_listeners()
        return self.build(strat_path, header)

    def build(self, strat_path: str, header: Optional[Header] = None) -> StratFile:
        events = self.coalescer.finish()
        geo = self._geo or self.window.get_geometry()
        hdr = header or Header()
        if not hdr.window_aspect:
            hdr.window_aspect = round(geo.aspect, 6)
        if not hdr.reference_resolution:
            hdr.reference_resolution = {"w": int(geo.w * geo.retina), "h": int(geo.h * geo.retina)}
        hdr.retina_scale_captured_at = geo.retina
        return StratFile(header=hdr, config_overrides={}, events=events,
                         recovery=RecoverySpec(), base_dir=self._strat_dir)
