"""Global hotkeys on a dedicated listener thread that ONLY sets Events (plan M9/R21).

The listener never does heavy work, so a panic always fires even while the engine
thread is busy in a capture/compare. The engine polls these Events between every
atomic action and inside every sleep/poll. An optional kill-switch file watcher
is a last-resort panic when synthetic-input listening is blocked (R21/R19).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

log = logging.getLogger("tds_macro.hotkeys")


@dataclass
class HotkeyEvents:
    panic: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    start: threading.Event = field(default_factory=threading.Event)
    mark_sync: threading.Event = field(default_factory=threading.Event)

    def is_panic(self) -> bool:
        return self.panic.is_set()

    def is_stop(self) -> bool:
        return self.panic.is_set() or self.stop.is_set()

    def should_abort(self) -> bool:
        """Single predicate handed to clock/input so a panic OR stop interrupts."""
        return self.panic.is_set() or self.stop.is_set()


def _to_pynput_combo(spec: str) -> str:
    """'f8' -> '<f8>', 'Ctrl+Alt+P' -> '<ctrl>+<alt>+p'.

    Lowercased (pynput Key/char lookup is case-sensitive, so 'F8' would be rejected),
    empty tokens dropped; returns '' if nothing usable.
    """
    parts = []
    for tok in (spec or "").split("+"):
        tok = tok.strip().lower()
        if not tok:
            continue
        parts.append(f"<{tok}>" if len(tok) > 1 else tok)
    return "+".join(parts)


class HotkeyManager:
    """Registers global hotkeys via pynput (lazy). No-ops cleanly if unavailable."""

    def __init__(self, config, events: HotkeyEvents | None = None) -> None:
        self.config = config
        self.events = events or HotkeyEvents()
        self._listener = None
        self._killswitch_thread = None
        self._killswitch_stop = threading.Event()

    def should_abort(self) -> bool:
        return self.events.should_abort()

    def is_panic(self) -> bool:
        return self.events.is_panic()

    def _on_panic(self):
        self.events.panic.set()
        self.events.stop.set()

    def _on_stop(self):
        self.events.stop.set()

    def _on_pause(self):
        if self.events.pause.is_set():
            self.events.pause.clear()
        else:
            self.events.pause.set()

    def _on_start(self):
        self.events.start.set()

    def _on_mark_sync(self):
        self.events.mark_sync.set()

    def start(self) -> bool:
        """Start global hotkeys. Returns True if the OS listener was installed."""
        # idempotent: a second start() without stop() would leak the old listener/thread
        # and re-arm the old watcher -> tear the previous one down first (recheck #w-dblstart)
        if self._listener is not None or (self._killswitch_thread is not None
                                          and self._killswitch_thread.is_alive()):
            self.stop()
        self._killswitch_stop.clear()  # re-arm if this manager is start()ed again after stop()
        installed = False
        try:
            from pynput import keyboard  # type: ignore

            specs = [
                ("panic", self.config.panic_hotkey, self._on_panic),
                ("pause", self.config.pause_hotkey, self._on_pause),
                ("start", self.config.start_hotkey, self._on_start),
                ("mark_sync", self.config.mark_sync_hotkey, self._on_mark_sync),
            ]
            # Register hotkeys individually + validate each, so ONE bad/misconfigured
            # combo can't take down all global hotkeys (incl. the safety-critical panic key).
            mapping = {}
            for name, spec, cb in specs:
                combo = _to_pynput_combo(spec)
                if not combo:
                    log.warning("hotkey %s=%r is empty; skipping", name, spec)
                    continue
                try:
                    keyboard.HotKey.parse(combo)  # raises on an invalid combo
                except Exception as e:
                    log.warning("hotkey %s=%r is invalid (%s); skipping (others stay active)", name, spec, e)
                    continue
                if combo in mapping:
                    # panic is registered first, so a duplicate combo skips the LATER
                    # binding and never overwrites the safety-critical panic key.
                    log.warning("hotkey %s=%r collides with an earlier binding (%s); skipping", name, spec, combo)
                    continue
                mapping[combo] = cb
            if mapping:
                self._listener = keyboard.GlobalHotKeys(mapping)
                self._listener.start()
                installed = True
        except ImportError:
            # No pynput (tests / non-mac): events are driven directly. Expected -> stay quiet.
            self._listener = None
        except Exception:
            # pynput IS present but the listener couldn't be built/started (permissions, OS, or a
            # combo GlobalHotKeys rejects): the safety-critical panic key is now disabled, so make
            # it visible rather than failing silently (round 21 #4). The file kill-switch (below)
            # remains the backstop if configured.
            log.warning("global hotkey listener failed to start; panic/stop hotkeys are DISABLED "
                        "(file kill-switch still active if configured)", exc_info=True)
            self._listener = None

        if self.config.killswitch_file:
            self._start_killswitch_watch()
        return installed

    def _start_killswitch_watch(self):
        path = self.config.killswitch_file
        # Clear any stale file so ONLY a file created after start() triggers panic
        # (otherwise a leftover from a prior panic makes every run instantly abort).
        try:
            os.remove(path)
        except OSError:
            pass

        def _watch():
            while not self._killswitch_stop.wait(0.2):
                try:
                    if os.path.exists(path):
                        self._on_panic()
                        return
                except Exception:
                    pass

        self._killswitch_thread = threading.Thread(target=_watch, daemon=True)
        self._killswitch_thread.start()

    def stop(self):
        self._killswitch_stop.set()
        # JOIN the watcher before returning so a later start()'s _killswitch_stop.clear()
        # can't leave an orphaned daemon polling the same Event/file (recheck #w-ks-join).
        t = self._killswitch_thread
        if t is not None:
            t.join(timeout=1.0)
            self._killswitch_thread = None
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
