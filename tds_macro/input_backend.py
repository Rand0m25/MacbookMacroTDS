"""Mouse/keyboard control + recording listeners behind one interface.

The real backend uses ``pynput`` (lazy import). Critical safety properties from
the plan:
  * release_all() is idempotent and lock-guarded; it snapshots held inputs under
    a lock then releases them outside it (S14), and is the thing the engine's
    ``finally`` calls on panic so no key/button is ever left stuck (M9/R22).
  * move()/drag() take ``should_abort`` and check it between every interpolation
    step, raising PanicAbort so a panic interrupts within one step (M10).
  * keys use a documented string codec with a round-trip (S9).
All coordinates passed here are ABSOLUTE LOGICAL POINTS (the engine converts
normalized -> logical via geometry just before calling).
"""

from __future__ import annotations

import threading
import logging
import sys
import time
from typing import Protocol

from .errors import PanicAbort

log = logging.getLogger("tds_macro.input_backend")


def prewarm_macos_keyboard() -> None:
    """Work around a pynput-vs-macOS-15 crash (SIGSEGV) — call ONCE on the MAIN thread.

    pynput's macOS keyboard ``Listener`` enters ``keycode_context()`` inside ``_run()``
    — i.e. on the *listener thread*. That contextmanager calls TIS input-source APIs
    (``TISCopyCurrentKeyboardInputSource`` -> ``islGetInputSourceListWithAdditions``);
    macOS 15 aborts the whole process (``dispatch_assert_queue`` -> SIGSEGV) when those
    run off the main thread, so Record/Play crash the instant a listener starts (pynput
    issue #511/#512). The assertion fires on EVERY off-main-thread call, so merely
    warming a cache doesn't help — the listener re-enters the context on its own thread.

    But ``keycode_context()`` only yields a plain ``(keyboard_type, layout_data)`` tuple,
    and the actual translation (``UCKeyTranslate``) needs no TIS call. So we build that
    tuple ONCE here on the main thread (safe — this is what ``Controller`` does) and
    monkeypatch ``keycode_context`` to yield the cached tuple, so the listener thread
    never touches TIS again. Keyboard translation still works. Idempotent; no-op off
    macOS / without pynput."""
    import sys

    if sys.platform != "darwin":
        return
    try:
        import contextlib

        from pynput.keyboard import _darwin as kbd

        if getattr(kbd, "_tds_keycode_patched", False):
            return  # already patched this process
        if not hasattr(kbd, "keycode_context"):
            return  # unexpected pynput layout — leave it alone rather than break it

        with kbd.keycode_context() as cached:  # build on the main thread (no assertion here)
            pass

        @contextlib.contextmanager
        def _cached_keycode_context():
            yield cached  # the listener thread reuses this; no TIS call -> no SIGSEGV

        kbd.keycode_context = _cached_keycode_context
        kbd._tds_keycode_patched = True
        # Some pynput versions resolve the name via the util module too; patch both.
        try:
            from pynput._util import darwin as _util_darwin
            _util_darwin.keycode_context = _cached_keycode_context
        except Exception:  # noqa: BLE001
            pass
        log.debug("patched pynput keycode_context for macOS main-thread safety")
    except Exception as e:  # noqa: BLE001 - never let a warm-up failure block startup
        log.debug("macOS keyboard pre-warm skipped: %s", e)


# --- key string codec (S9) ----------------------------------------------------
# Stable lowercase names <-> pynput Key members. Single characters map to chars.
_SPECIAL_NAMES = (
    "esc enter space tab backspace delete up down left right home end page_up "
    "page_down shift shift_r ctrl ctrl_r alt alt_r cmd cmd_r caps_lock "
    "f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12"
).split()


# macOS virtual keycodes for the NUMBER ROW (kVK_ANSI_1..0). Used to force the number-row key for plain
# digits, since pynput's char->keycode map on macOS can resolve "1".."0" to the NUMERIC KEYPAD instead.
_MACOS_NUMBER_ROW_VK = {
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
}


def key_to_pynput(name: str):
    from pynput.keyboard import Key, KeyCode  # type: ignore

    if name in _SPECIAL_NAMES:
        return getattr(Key, name)
    if name.startswith("vk:"):  # lossless round-trip of vk-only keys
        try:
            return KeyCode.from_vk(int(name[3:]))
        except (ValueError, AttributeError):
            pass
    if sys.platform == "darwin" and name in _MACOS_NUMBER_ROW_VK:
        # On macOS pynput's unicode->keycode map resolves the digit CHARACTERS to the NUMERIC KEYPAD
        # keycodes, so a game bound to the number ROW (e.g. the TDS tower hotbar 1-9) ignores them and
        # the tower is never selected. Force the number-row virtual keycode instead (observed bug).
        try:
            return KeyCode.from_vk(_MACOS_NUMBER_ROW_VK[name])
        except (ValueError, AttributeError):
            pass
    if len(name) == 1:
        return name
    # tolerate names like "Key.space"
    short = name.replace("Key.", "")
    if hasattr(Key, short):
        return getattr(Key, short)
    # unrecognized (multi-char dead-key/IME grapheme, or empty): skip it rather than inject a
    # wrong character. Callers below treat None as "no key" (recheck #w-keynone).
    log.warning("unrecognized key %r; skipping it", name)
    return None


def pynput_to_name(key) -> str:
    char = getattr(key, "char", None)
    if char:
        return char
    s = str(key)
    if s.startswith("Key."):
        return s[len("Key."):]
    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"vk:{vk}"  # encode vk-only keys stably so replay reconstructs them
    return s


def _button_name(button) -> str:
    return str(button).replace("Button.", "")


class InputBackend(Protocol):
    def move(self, px, py, duration_ms=0, hz=120, clock=None, should_abort=None, easing="linear"): ...
    def click(self, button="left", px=None, py=None, clicks=1, hold_ms=25): ...
    def drag(self, button, fx, fy, tx, ty, duration_ms=300, hz=120, clock=None, should_abort=None): ...
    def press_key(self, key, modifiers=()): ...
    def release_key(self, key, modifiers=()): ...
    def scroll(self, dx, dy): ...
    def position(self) -> tuple[float, float]: ...
    def start_listeners(self, on_move=None, on_click=None, on_scroll=None, on_press=None, on_release=None): ...
    def stop_listeners(self): ...
    def release_all(self): ...


_MAX_LERP_STEPS = 10000  # cap so an absurd hand-edited duration_ms can't make the mock busy-spin (round 22b #5)


def _lerp_steps(duration_ms: float, hz: int) -> int:
    return min(_MAX_LERP_STEPS, max(1, int(round(duration_ms / 1000.0 * hz))))


def _ease(name: str, t: float) -> float:
    """Map a linear progress t in [0,1] to an eased value. 'linear' is the identity, so existing
    (linear) recordings move EXACTLY as before; other curves humanize motion (round 22b #4)."""
    if name == "ease_in":
        return t * t
    if name == "ease_out":
        return t * (2.0 - t)
    if name == "ease_in_out":
        return t * t * (3.0 - 2.0 * t)  # smoothstep
    return t  # linear / unknown


class MockInputBackend:
    """Records every injected action; models held state + abortable drags."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.held_buttons: set[str] = set()
        self.held_keys: set[str] = set()
        self._pos = (0.0, 0.0)
        self._lock = threading.Lock()
        # listener callbacks (for recorder tests)
        self.on_move = self.on_click = self.on_scroll = None
        self.on_press = self.on_release = None

    def move(self, px, py, duration_ms=0, hz=120, clock=None, should_abort=None, easing="linear"):
        steps = _lerp_steps(duration_ms, hz) if duration_ms > 0 else 1
        for _ in range(steps):
            if should_abort and should_abort():
                raise PanicAbort("panic during move")
        self._pos = (px, py)
        self.events.append({"action": "move", "x": px, "y": py, "duration_ms": duration_ms, "easing": easing})

    def click(self, button="left", px=None, py=None, clicks=1, hold_ms=25):
        if px is not None and py is not None:
            self.move(px, py)
        self.events.append({"action": "click", "button": button, "x": self._pos[0], "y": self._pos[1], "clicks": clicks, "hold_ms": hold_ms})

    def drag(self, button, fx, fy, tx, ty, duration_ms=300, hz=120, clock=None, should_abort=None):
        self.move(fx, fy)
        with self._lock:
            self.held_buttons.add(button)
        self.events.append({"action": "press", "button": button, "x": fx, "y": fy})
        steps = _lerp_steps(duration_ms, hz)
        for i in range(1, steps + 1):
            if should_abort and should_abort():
                raise PanicAbort("panic during drag")  # button intentionally left held
            t = i / steps
            self._pos = (fx + (tx - fx) * t, fy + (ty - fy) * t)
        with self._lock:
            self.held_buttons.discard(button)
        self.events.append({"action": "release", "button": button, "x": tx, "y": ty})

    def press_key(self, key, modifiers=()):
        with self._lock:
            self.held_keys.add(key)
        self.events.append({"action": "key_press", "key": key, "modifiers": list(modifiers)})

    def release_key(self, key, modifiers=()):
        with self._lock:
            self.held_keys.discard(key)
        self.events.append({"action": "key_release", "key": key, "modifiers": list(modifiers)})

    def scroll(self, dx, dy):
        self.events.append({"action": "scroll", "dx": dx, "dy": dy})

    def position(self):
        return self._pos

    def start_listeners(self, on_move=None, on_click=None, on_scroll=None, on_press=None, on_release=None):
        self.on_move, self.on_click, self.on_scroll = on_move, on_click, on_scroll
        self.on_press, self.on_release = on_press, on_release

    def stop_listeners(self):
        self.on_move = self.on_click = self.on_scroll = self.on_press = self.on_release = None

    def release_all(self):
        with self._lock:
            buttons = list(self.held_buttons)
            keys = list(self.held_keys)
            self.held_buttons.clear()
            self.held_keys.clear()
        for b in buttons:
            self.events.append({"action": "release", "button": b, "reason": "release_all"})
        for k in keys:
            self.events.append({"action": "key_release", "key": k, "reason": "release_all"})

    # --- helpers for recorder tests: simulate raw OS events ---
    def feed_move(self, x, y):
        if self.on_move:
            self.on_move(x, y)

    def feed_click(self, x, y, button="left", pressed=True):
        if self.on_click:
            self.on_click(x, y, button, pressed)

    def feed_key(self, key, pressed=True):
        cb = self.on_press if pressed else self.on_release
        if cb:
            cb(key)

    def feed_scroll(self, x, y, dx, dy):
        if self.on_scroll:
            self.on_scroll(x, y, dx, dy)


class PynputInputBackend:
    """Real macOS backend via pynput (lazy import)."""

    def __init__(self) -> None:
        self._mouse = None
        self._kb = None
        self._lock = threading.Lock()
        self._held_buttons: set[str] = set()
        self._held_keys: dict[str, int] = {}  # key -> refcount (a modifier shared by two held keys) (round 22 #M)
        self._mouse_listener = None
        self._kb_listener = None

    def _hold(self, k: str) -> None:
        with self._lock:
            self._held_keys[k] = self._held_keys.get(k, 0) + 1

    def _unhold(self, k: str) -> bool:
        """Decrement k's refcount; return True iff it just dropped to 0 (was held) -> OS-release it."""
        with self._lock:
            n = self._held_keys.get(k, 0)
            if n <= 1:
                self._held_keys.pop(k, None)
                return n >= 1
            self._held_keys[k] = n - 1
            return False

    def _ensure(self):
        if self._mouse is None:
            from pynput.mouse import Controller as MouseController  # type: ignore
            from pynput.keyboard import Controller as KeyController  # type: ignore

            self._mouse = MouseController()
            self._kb = KeyController()

    def _button(self, name: str):
        from pynput.mouse import Button  # type: ignore

        return getattr(Button, name)

    def move(self, px, py, duration_ms=0, hz=120, clock=None, should_abort=None, easing="linear"):
        self._ensure()
        if duration_ms <= 0:
            self._mouse.position = (px, py)
            return
        sx, sy = self._mouse.position
        steps = _lerp_steps(duration_ms, hz)
        for i in range(1, steps + 1):
            if should_abort and should_abort():
                raise PanicAbort("panic during move")
            t = _ease(easing, i / steps)  # 'linear' is identity -> unchanged for existing strats (round 22b #4)
            self._mouse.position = (sx + (px - sx) * t, sy + (py - sy) * t)
            self._sleep(duration_ms / steps, clock)

    @staticmethod
    def _sleep(ms, clock):
        if clock is not None:
            clock.sleep(ms)
        else:
            time.sleep(max(0.0, ms) / 1000.0)

    def click(self, button="left", px=None, py=None, clicks=1, hold_ms=25):
        self._ensure()
        btn = self._button(button)
        if px is not None and py is not None:
            self._mouse.position = (px, py)
        # Track the button as held BEFORE the OS call and discard only AFTER it returns
        # normally, so a mid-call raise leaves it tracked for release_all() to recover
        # (releasing an already-up button is harmless) — recheck #w-click.
        if clicks >= 2:
            with self._lock:
                self._held_buttons.add(button)
            self._mouse.click(btn, clicks)  # true double-click semantics on macOS (press+release internally)
            with self._lock:
                self._held_buttons.discard(button)
            return
        with self._lock:
            self._held_buttons.add(button)
        self._mouse.press(btn)
        time.sleep(max(0.0, hold_ms) / 1000.0)
        self._mouse.release(btn)
        with self._lock:
            self._held_buttons.discard(button)

    def drag(self, button, fx, fy, tx, ty, duration_ms=300, hz=120, clock=None, should_abort=None):
        self._ensure()
        btn = self._button(button)
        self._mouse.position = (fx, fy)
        with self._lock:
            self._held_buttons.add(button)
        self._mouse.press(btn)
        steps = _lerp_steps(duration_ms, hz)
        try:
            for i in range(1, steps + 1):
                if should_abort and should_abort():
                    raise PanicAbort("panic during drag")
                t = i / steps
                self._mouse.position = (fx + (tx - fx) * t, fy + (ty - fy) * t)
                self._sleep(duration_ms / steps, clock)
        finally:
            self._mouse.release(btn)
            with self._lock:
                self._held_buttons.discard(button)

    def press_key(self, key, modifiers=()):
        self._ensure()
        # Record each key BEFORE pressing it, so a mid-sequence exception still
        # leaves what was physically pressed known to release_all (no stuck key).
        # An unmappable key (key_to_pynput -> None) is skipped, not tracked (recheck #w-keynone).
        for m in modifiers:
            pm = key_to_pynput(m)
            if pm is None:
                continue
            self._hold(m)  # track (refcount) BEFORE pressing for crash-safety
            self._kb.press(pm)
        pk = key_to_pynput(key)
        if pk is None:
            return
        self._hold(key)
        self._kb.press(pk)

    def release_key(self, key, modifiers=()):
        self._ensure()
        # Release the main key first, then modifiers. Only OS-release a key whose refcount is about to
        # hit 0 (a modifier still held by another key stays down) — round 22 #M. Untrack only AFTER a
        # successful OS release, so a release that raises leaves the key tracked for release_all() to
        # recover instead of becoming stuck input (round 23 #7).
        for k in (key, *modifiers):
            pk = key_to_pynput(k)
            with self._lock:
                last = self._held_keys.get(k, 0) <= 1
            if pk is not None and last:
                try:
                    self._kb.release(pk)
                except Exception:
                    continue  # keep k tracked; release_all() will retry it
            self._unhold(k)

    def scroll(self, dx, dy):
        self._ensure()
        self._mouse.scroll(dx, dy)

    def position(self):
        self._ensure()
        return self._mouse.position

    def start_listeners(self, on_move=None, on_click=None, on_scroll=None, on_press=None, on_release=None):
        from pynput import mouse, keyboard  # type: ignore

        def _m_move(x, y):
            if on_move:
                on_move(x, y)

        def _m_click(x, y, button, pressed):
            if on_click:
                on_click(x, y, _button_name(button), pressed)

        def _m_scroll(x, y, dx, dy):
            if on_scroll:
                on_scroll(x, y, dx, dy)

        def _k_press(key):
            if on_press:
                on_press(pynput_to_name(key))

        def _k_release(key):
            if on_release:
                on_release(pynput_to_name(key))

        self._mouse_listener = mouse.Listener(on_move=_m_move, on_click=_m_click, on_scroll=_m_scroll)
        self._kb_listener = keyboard.Listener(on_press=_k_press, on_release=_k_release)
        self._mouse_listener.start()
        self._kb_listener.start()

    def stop_listeners(self):
        for lst in (self._mouse_listener, self._kb_listener):
            if lst is not None:
                try:
                    lst.stop()
                    # JOIN so no in-flight callback can fire after the recorder stops and drains its
                    # held keys/buttons (else a late _on_press appends an unpaired event) (round 22b #7)
                    lst.join(timeout=1.0)
                except Exception:
                    pass
        self._mouse_listener = self._kb_listener = None

    def release_all(self):
        self._ensure()
        with self._lock:
            buttons = list(self._held_buttons)
            keys = list(self._held_keys)
            self._held_buttons.clear()
            self._held_keys.clear()
        for b in buttons:
            try:
                self._mouse.release(self._button(b))
            except Exception:
                pass
        for k in keys:
            pk = key_to_pynput(k)
            if pk is None:
                continue
            try:
                self._kb.release(pk)
            except Exception:
                pass


def make_input_backend(config) -> InputBackend:
    from .config import InputBackendKind

    if config.input_backend == InputBackendKind.MOCK:
        return MockInputBackend()
    return PynputInputBackend()
