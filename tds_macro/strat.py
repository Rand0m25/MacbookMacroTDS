"""Human-editable JSON strat model + a registry-based validator.

No Pydantic: a ``TYPE -> dataclass`` registry parses the discriminated Event
union, checking required keys, types, normalized-coord ranges, unknown keys, and
enums, and collecting ALL problems before raising (plan M12/R24). Reference PNGs
are existence-checked and their dimensions read from the 8-byte IHDR via stdlib
``struct`` (no Pillow). Saves are atomic (temp in same dir + fsync + os.replace,
S11). ``expand_macro`` turns TDS macros into primitives with ABSOLUTE t_ms so
their internal waits stretch under lag with the clock rebase (plan M8).

Schema extras vs a naive macro: ``join_sequence`` (M14), ``run_end`` (M15),
``expected_map_check`` (M16), per-action ``expect`` (S12), sync/recovery
``mask`` (S1).
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from .config import MatchMethod, looks_like_roblox_url
from .errors import StratValidationError
from .geometry import Point, Rect

log = logging.getLogger("tds_macro.strat")

SCHEMA_VERSION = 1

ON_TIMEOUT = {"abort", "continue", "retry", "recover"}
RECOVERY_ACTIONS = {"leave_and_restart", "reconnect_and_rejoin", "reset_and_rejoin", "stop"}
BUTTONS = {"left", "right", "middle"}
EASINGS = {"linear", "ease_in", "ease_out", "ease_in_out"}  # implemented in input_backend._ease

# coord tolerance: recorded points may sit a hair outside the window
_CMIN, _CMAX = -0.05, 1.05

# macro-expansion internal gaps (ms); absolute t_ms so they rebase under lag (M8)
_KEY_GAP = 40
_MOVE_DUR = 90
_POST_MOVE = 20
_PANEL_GAP = 180


# --------------------------------------------------------------------------- #
# validation helpers (append to a problems list with a context string)
# --------------------------------------------------------------------------- #
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _req(d: dict, key: str, ctx: str, problems: list) -> bool:
    if key not in d:
        problems.append(f"{ctx}: missing required field '{key}'")
        return False
    return True


def _req_str(d: dict, key: str, ctx: str, problems: list) -> str:
    """Require a STRING field. A non-string (e.g. a numeric ref_frame) would later
    crash os.path.isabs/join, so report it as a clean problem and substitute ''
    (recheck #w12)."""
    if not _req(d, key, ctx, problems):
        return ""
    v = d.get(key, "")
    if not isinstance(v, str):
        problems.append(f"{ctx}: '{key}' must be a string")
        return ""
    return v


def _coord(v, name: str, ctx: str, problems: list) -> float:
    if not _is_num(v):
        problems.append(f"{ctx}: '{name}' must be a number, got {type(v).__name__}")
        return 0.0
    try:
        f = float(v)  # float() of an absurd ~400-digit int raises OverflowError
    except OverflowError:
        problems.append(f"{ctx}: '{name}' is too large")
        return 0.0
    if not (_CMIN <= f <= _CMAX):
        problems.append(f"{ctx}: '{name}'={f} is outside normalized range [0,1]")
    return f


_MAX_NUM = 10 ** 12  # sane ceiling for any event field (ms/counts): ~31 years of ms; rejects 1e300


def _num(v, default, name: str, ctx: str, problems: list, cast=float, lo=None, hi=None):
    """Guarded numeric coercion for JSON values.

    Missing/empty -> default (None default stays None, for optional fields).
    Present-but-non-numeric (incl. bool, via _is_num) -> append a problem and fall back to default.
    ``lo``/``hi`` (inclusive) bound the value: every numeric event field passes its range here so a
    hand-edited absurd value is reported at load instead of misbehaving at playback (one source of
    truth — round 22d systematic pass). Out-of-range falls back to the default like other rejects.
    """
    if v is None or v == "":
        return None if default is None else cast(default)
    if not _is_num(v):
        problems.append(f"{ctx}: '{name}' must be a number, got {type(v).__name__}")
        return None if default is None else cast(default)
    if isinstance(v, float) and not math.isfinite(v):
        # json.loads accepts NaN/Infinity; reject them rather than crash int(inf)
        problems.append(f"{ctx}: '{name}' must be a finite number, got {v}")
        return None if default is None else cast(default)
    if cast is int and isinstance(v, float) and not v.is_integer():
        # a fractional float in an int field (t_ms/id/...) would silently truncate (recheck #w11)
        problems.append(f"{ctx}: '{name}' must be a whole number, got {v}")
        return None if default is None else cast(default)
    try:
        result = cast(v)  # float() of a huge Python int overflows
    except OverflowError:
        problems.append(f"{ctx}: '{name}' is too large")
        return None if default is None else cast(default)
    if abs(result) > _MAX_NUM:  # int(1e300) succeeds -> a huge t_ms would hang sleep_until forever (round 22b #3)
        problems.append(f"{ctx}: '{name}'={v} is out of range")
        return None if default is None else cast(default)
    if lo is not None and result < lo:
        problems.append(f"{ctx}: '{name}'={result} must be >= {lo}")
        return None if default is None else cast(default)
    if hi is not None and result > hi:
        problems.append(f"{ctx}: '{name}'={result} must be <= {hi}")
        return None if default is None else cast(default)
    return result


def _threshold(v, default, ctx: str, problems: list):
    """Parse a match threshold and require it in [0,1]. score() (visual.py) is clamped to
    [0,1] and the engine matches via ``score >= threshold``, so a hand-edited threshold > 1
    can never match (every sync_point times out -> spurious recover/abort) and one < 0 always
    matches (syncs fire on the wrong screen). Bound it at parse time (round 20 #2)."""
    t = _num(v, default, "threshold", ctx, problems, cast=float)
    if t is not None and not (0.0 <= t <= 1.0):
        problems.append(f"{ctx}: 'threshold'={t} must be in [0.0, 1.0]")
    return t


def _safe_float(v, default: float) -> float:
    """Silent numeric coercion for cosmetic header fields (no crash on bad input)."""
    if isinstance(v, bool) or v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):  # OverflowError: huge int
        return default
    return f if math.isfinite(f) else default


def _point(d, name, ctx, problems, required=True) -> Optional[Point]:
    if name not in d:
        if required:
            problems.append(f"{ctx}: missing required point '{name}'")
        return None
    pd = d[name]
    if not isinstance(pd, dict) or "x" not in pd or "y" not in pd:
        problems.append(f"{ctx}: '{name}' must be an object with x and y")
        return None
    return Point(_coord(pd.get("x"), f"{name}.x", ctx, problems),
                 _coord(pd.get("y"), f"{name}.y", ctx, problems))


def _rect(d, name, ctx, problems, required=True) -> Optional[Rect]:
    if name not in d:
        if required:
            problems.append(f"{ctx}: missing required rect '{name}'")
        return None
    rd = d[name]
    if not isinstance(rd, dict) or not all(k in rd for k in ("x", "y", "w", "h")):
        problems.append(f"{ctx}: '{name}' must be an object with x,y,w,h")
        return None
    r = Rect(_coord(rd.get("x"), f"{name}.x", ctx, problems),
             _coord(rd.get("y"), f"{name}.y", ctx, problems),
             _coord(rd.get("w"), f"{name}.w", ctx, problems),
             _coord(rd.get("h"), f"{name}.h", ctx, problems))
    if r.w <= 0 or r.h <= 0:
        problems.append(f"{ctx}: '{name}' width/height must be > 0")
    return r


def _mask(d, ctx, problems) -> list[Rect]:
    out: list[Rect] = []
    raw = d.get("mask")
    if raw is None:
        return out
    if not isinstance(raw, list):
        problems.append(f"{ctx}: 'mask' must be a list of rects")
        return out
    for i, rd in enumerate(raw):
        r = _rect({"m": rd}, "m", f"{ctx}.mask[{i}]", problems)
        if r:
            out.append(r)
    return out


def _enum(v, allowed: set, name: str, ctx: str, problems: list, default=None):
    if v is None:
        return default
    if not isinstance(v, str):  # unhashable (list/dict) would crash the `in` test
        problems.append(f"{ctx}: '{name}' must be a string, got {type(v).__name__}")
        return default
    if v not in allowed:
        problems.append(f"{ctx}: '{name}'={v!r} not one of {sorted(allowed)}")
        return default
    return v


def _bool(v, default: bool, name: str, ctx: str, problems: list) -> bool:
    """Strict JSON boolean: a quoted "false"/"no" is a reported typo, not silently
    truthy (raw bool('false') is True -> would invert the user's intent)."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    problems.append(f"{ctx}: '{name}' must be a boolean (true/false), got {type(v).__name__}")
    return default


def _no_unknown(d: dict, allowed: set, ctx: str, problems: list) -> None:
    extra = set(d.keys()) - allowed
    if extra:
        problems.append(f"{ctx}: unknown field(s) {sorted(extra)}")


def png_dimensions(path: str) -> tuple[int, int]:
    """Read (width, height) from a PNG's IHDR chunk using only stdlib."""
    with open(path, "rb") as f:
        sig = f.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            raise ValueError("not a PNG file")
        f.read(4)  # IHDR length
        if f.read(4) != b"IHDR":
            raise ValueError("missing IHDR chunk")
        w, h = struct.unpack(">II", f.read(8))
        return int(w), int(h)


# --------------------------------------------------------------------------- #
# event dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    id: int
    t_ms: int
    type: str
    comment: str = ""
    jitter_ms: Optional[int] = None  # None == use config.jitter_ms; an explicit 0 suppresses it (round 22c #7)

    def base_dict(self) -> dict:
        d = {"id": self.id, "t_ms": self.t_ms, "type": self.type}
        if self.comment:
            d["comment"] = self.comment
        if self.jitter_ms is not None:  # round-trip an explicit value (incl. 0), omit when absent
            d["jitter_ms"] = self.jitter_ms
        return d


@dataclass
class WaitEvent(Event):
    duration_ms: int = 0
    reason: str = ""

    def to_dict(self):
        d = self.base_dict()
        d["duration_ms"] = self.duration_ms
        if self.reason:
            d["reason"] = self.reason
        return d


@dataclass
class MouseMoveEvent(Event):
    pos: Point = field(default_factory=lambda: Point(0, 0))
    duration_ms: int = 0
    easing: str = "linear"

    def to_dict(self):
        d = self.base_dict()
        d["pos"] = self.pos.to_dict()
        d["duration_ms"] = self.duration_ms
        d["easing"] = self.easing
        return d


@dataclass
class ClickEvent(Event):
    button: str = "left"
    pos: Optional[Point] = None
    clicks: int = 1
    hold_ms: int = 0

    def to_dict(self):
        d = self.base_dict()
        d["button"] = self.button
        if self.pos is not None:
            d["pos"] = self.pos.to_dict()
        d["clicks"] = self.clicks
        if self.hold_ms:
            d["hold_ms"] = self.hold_ms
        return d


@dataclass
class DragEvent(Event):
    button: str = "left"
    frm: Point = field(default_factory=lambda: Point(0, 0))
    to: Point = field(default_factory=lambda: Point(0, 0))
    duration_ms: int = 300

    def to_dict(self):
        d = self.base_dict()
        d["button"] = self.button
        d["from"] = self.frm.to_dict()
        d["to"] = self.to.to_dict()
        d["duration_ms"] = self.duration_ms
        return d


@dataclass
class KeyPressEvent(Event):
    key: str = ""
    modifiers: list = field(default_factory=list)

    def to_dict(self):
        d = self.base_dict()
        d["key"] = self.key
        if self.modifiers:
            d["modifiers"] = list(self.modifiers)
        return d


@dataclass
class KeyReleaseEvent(Event):
    key: str = ""
    modifiers: list = field(default_factory=list)

    def to_dict(self):
        d = self.base_dict()
        d["key"] = self.key
        if self.modifiers:
            d["modifiers"] = list(self.modifiers)
        return d


@dataclass
class ScrollEvent(Event):
    pos: Optional[Point] = None
    dx: int = 0
    dy: int = 0

    def to_dict(self):
        d = self.base_dict()
        if self.pos is not None:
            d["pos"] = self.pos.to_dict()
        d["dx"] = self.dx
        d["dy"] = self.dy
        return d


@dataclass
class SyncPointEvent(Event):
    label: str = ""
    ref_frame: str = ""
    region: Rect = field(default_factory=lambda: Rect(0, 0, 1, 1))
    threshold: Optional[float] = None
    timeout_ms: Optional[int] = None
    on_timeout: str = "abort"
    match: Optional[MatchMethod] = None
    poll_ms: Optional[int] = None
    stability_frames: Optional[int] = None
    mask: list = field(default_factory=list)
    require_settled: bool = False

    def to_dict(self):
        d = self.base_dict()
        d.update({"label": self.label, "ref_frame": self.ref_frame, "region": self.region.to_dict(),
                  "on_timeout": self.on_timeout})
        if self.threshold is not None:
            d["threshold"] = self.threshold
        if self.timeout_ms is not None:
            d["timeout_ms"] = self.timeout_ms
        if self.match is not None:
            d["match"] = self.match.value
        if self.poll_ms is not None:
            d["poll_ms"] = self.poll_ms
        if self.stability_frames is not None:
            d["stability_frames"] = self.stability_frames
        if self.mask:
            d["mask"] = [m.to_dict() for m in self.mask]
        if self.require_settled:
            d["require_settled"] = True
        return d


@dataclass
class ExpectSpec:
    ref_frame: str
    region: Rect
    threshold: float = 0.9
    timeout_ms: int = 4000

    def to_dict(self):
        return {"ref_frame": self.ref_frame, "region": self.region.to_dict(),
                "threshold": self.threshold, "timeout_ms": self.timeout_ms}


@dataclass
class PlaceTowerEvent(Event):
    tower: str = ""
    hotbar_slot: int = 1
    pos: Point = field(default_factory=lambda: Point(0, 0))
    settle_ms: int = 250
    confirm_click: bool = True
    expect: Optional[ExpectSpec] = None

    def to_dict(self):
        d = self.base_dict()
        d.update({"tower": self.tower, "hotbar_slot": self.hotbar_slot, "pos": self.pos.to_dict(),
                  "settle_ms": self.settle_ms, "confirm_click": self.confirm_click})
        if self.expect:
            d["expect"] = self.expect.to_dict()
        return d


@dataclass
class UpgradeEvent(Event):
    target_pos: Point = field(default_factory=lambda: Point(0, 0))
    upgrade_button_pos: Point = field(default_factory=lambda: Point(0, 0))
    times: int = 1
    between_ms: int = 300
    expect: Optional[ExpectSpec] = None

    def to_dict(self):
        d = self.base_dict()
        d.update({"target_pos": self.target_pos.to_dict(),
                  "upgrade_button_pos": self.upgrade_button_pos.to_dict(),
                  "times": self.times, "between_ms": self.between_ms})
        if self.expect:
            d["expect"] = self.expect.to_dict()
        return d


@dataclass
class AbilityEvent(Event):
    tower_pos: Point = field(default_factory=lambda: Point(0, 0))
    ability_button_pos: Point = field(default_factory=lambda: Point(0, 0))
    confirm: bool = False
    confirm_pos: Optional[Point] = None

    def to_dict(self):
        d = self.base_dict()
        d.update({"tower_pos": self.tower_pos.to_dict(),
                  "ability_button_pos": self.ability_button_pos.to_dict(), "confirm": self.confirm})
        if self.confirm_pos is not None:
            d["confirm_pos"] = self.confirm_pos.to_dict()
        return d


PRIMITIVE_TYPES = {"wait", "mouse_move", "click", "drag", "key_press", "key_release", "scroll", "sync_point"}
MACRO_TYPES = {"place_tower", "upgrade", "ability"}


# --------------------------------------------------------------------------- #
# per-type parsing (the registry)
# --------------------------------------------------------------------------- #
_BASE_KEYS = {"id", "t_ms", "type", "comment", "jitter_ms"}


def _base(d, ctx, problems):
    eid = _num(d.get("id"), 0, "id", ctx, problems, cast=int, lo=0)
    # lo=0: a negative t_ms would reorder events in expand_all's sort (round 22c #4)
    t = _num(d.get("t_ms"), 0, "t_ms", ctx, problems, cast=int, lo=0)
    # None default (not 0) so an explicit jitter_ms:0 can suppress global jitter for this event (round 22c #7)
    jitter = _num(d.get("jitter_ms"), None, "jitter_ms", ctx, problems, cast=int, lo=0)
    return eid, t, d.get("comment", ""), jitter


def _expect(d, ctx, problems) -> Optional[ExpectSpec]:
    if "expect" not in d:
        return None
    e = d["expect"]
    if not isinstance(e, dict):
        problems.append(f"{ctx}: 'expect' must be an object")
        return None
    _no_unknown(e, {"ref_frame", "region", "threshold", "timeout_ms"}, f"{ctx}.expect", problems)
    rf = _req_str(e, "ref_frame", f"{ctx}.expect", problems)
    region = _rect(e, "region", f"{ctx}.expect", problems)
    return ExpectSpec(rf, region or Rect(0, 0, 1, 1),
                      _threshold(e.get("threshold"), 0.9, f"{ctx}.expect", problems),
                      _num(e.get("timeout_ms"), 4000, "timeout_ms", f"{ctx}.expect", problems, cast=int, lo=1))


def _build_event(d: dict, ctx: str, problems: list) -> Optional[Event]:
    if not isinstance(d, dict):
        problems.append(f"{ctx}: event must be an object")
        return None
    typ = d.get("type")
    if typ is None:
        problems.append(f"{ctx}: missing 'type'")
        return None
    eid, t, comment, jitter = _base(d, ctx, problems)
    ctx = f"{ctx} (id={eid}, type={typ})"

    if typ == "wait":
        _no_unknown(d, _BASE_KEYS | {"duration_ms", "reason"}, ctx, problems)
        return WaitEvent(eid, t, typ, comment, jitter,
                         _num(d.get("duration_ms"), 0, "duration_ms", ctx, problems, cast=int, lo=0),
                         d.get("reason", ""))
    if typ == "mouse_move":
        _no_unknown(d, _BASE_KEYS | {"pos", "duration_ms", "easing"}, ctx, problems)
        p = _point(d, "pos", ctx, problems)
        return MouseMoveEvent(eid, t, typ, comment, jitter, p or Point(0, 0),
                              _num(d.get("duration_ms"), 0, "duration_ms", ctx, problems, cast=int, lo=0),
                              _enum(d.get("easing"), EASINGS, "easing", ctx, problems, default="linear"))
    if typ == "click":
        _no_unknown(d, _BASE_KEYS | {"button", "pos", "clicks", "hold_ms"}, ctx, problems)
        p = _point(d, "pos", ctx, problems, required=False)
        return ClickEvent(eid, t, typ, comment, jitter,
                          _enum(d.get("button"), BUTTONS, "button", ctx, problems, default="left"), p,
                          _num(d.get("clicks"), 1, "clicks", ctx, problems, cast=int, lo=1, hi=20),
                          # cap the hold so a hand-edited huge hold_ms can't block panic during the
                          # uninterruptible click hold for minutes (round 23 #8)
                          _num(d.get("hold_ms"), 0, "hold_ms", ctx, problems, cast=int, lo=0, hi=2000))
    if typ == "drag":
        _no_unknown(d, _BASE_KEYS | {"button", "from", "to", "duration_ms"}, ctx, problems)
        frm = _point(d, "from", ctx, problems)
        to = _point(d, "to", ctx, problems)
        return DragEvent(eid, t, typ, comment, jitter,
                         _enum(d.get("button"), BUTTONS, "button", ctx, problems, default="left"),
                         frm or Point(0, 0), to or Point(0, 0),
                         _num(d.get("duration_ms"), 300, "duration_ms", ctx, problems, cast=int, lo=0))
    if typ in ("key_press", "key_release"):
        _no_unknown(d, _BASE_KEYS | {"key", "modifiers"}, ctx, problems)
        key = _req_str(d, "key", ctx, problems)  # type-check like ref_frame; a list/dict key would
        mods = d.get("modifiers", []) or []      # stringify to junk and silently drop at playback (round 22c #5)
        if not isinstance(mods, list):
            problems.append(f"{ctx}: 'modifiers' must be a list")
            mods = []
        elif any(not isinstance(m, str) for m in mods):
            # a non-str modifier (e.g. 123) crashes key_to_pynput(123).startswith at playback (round 23 #2)
            problems.append(f"{ctx}: 'modifiers' must be a list of strings")
            mods = [m for m in mods if isinstance(m, str)]
        cls = KeyPressEvent if typ == "key_press" else KeyReleaseEvent
        return cls(eid, t, typ, comment, jitter, key, list(mods))
    if typ == "scroll":
        _no_unknown(d, _BASE_KEYS | {"pos", "dx", "dy"}, ctx, problems)
        p = _point(d, "pos", ctx, problems, required=False)
        return ScrollEvent(eid, t, typ, comment, jitter, p,  # sane caps; deltas may be negative (round 23 #1)
                           _num(d.get("dx"), 0, "dx", ctx, problems, cast=int, lo=-10000, hi=10000),
                           _num(d.get("dy"), 0, "dy", ctx, problems, cast=int, lo=-10000, hi=10000))
    if typ == "sync_point":
        _no_unknown(d, _BASE_KEYS | {"label", "ref_frame", "region", "threshold", "timeout_ms",
                                     "on_timeout", "match", "poll_ms", "stability_frames",
                                     "mask", "require_settled"}, ctx, problems)
        rf = _req_str(d, "ref_frame", ctx, problems)
        region = _rect(d, "region", ctx, problems)
        on_to = _enum(d.get("on_timeout"), ON_TIMEOUT, "on_timeout", ctx, problems, default="abort")
        match = None
        if "match" in d:
            try:
                match = MatchMethod(d["match"])
            except ValueError:
                problems.append(f"{ctx}: 'match'={d['match']!r} is not a known method")
        label = d.get("label", "")
        if isinstance(label, str) and label.startswith("expect_"):
            # 'expect_' is reserved for auto-generated action-verify syncs; the engine routes a
            # timeout on those to OUT_OF_CASH, so a user label like 'expect_boss' would be
            # misclassified instead of going through recovery.classify() (round 22b #2)
            problems.append(f"{ctx}: 'label' must not start with the reserved prefix 'expect_'")
        return SyncPointEvent(eid, t, typ, comment, jitter, label, rf,
                              region or Rect(0, 0, 1, 1),
                              _threshold(d.get("threshold"), None, ctx, problems),
                              _num(d.get("timeout_ms"), None, "timeout_ms", ctx, problems, cast=int, lo=1),
                              on_to, match,
                              _num(d.get("poll_ms"), None, "poll_ms", ctx, problems, cast=int, lo=1),
                              _num(d.get("stability_frames"), None, "stability_frames", ctx, problems, cast=int, lo=1),
                              _mask(d, ctx, problems),
                              _bool(d.get("require_settled"), False, "require_settled", ctx, problems))
    if typ == "place_tower":
        _no_unknown(d, _BASE_KEYS | {"tower", "hotbar_slot", "pos", "settle_ms", "confirm_click", "expect"}, ctx, problems)
        p = _point(d, "pos", ctx, problems)
        slot = _num(d.get("hotbar_slot"), 1, "hotbar_slot", ctx, problems, cast=int, lo=1, hi=8)
        settle = _num(d.get("settle_ms"), 250, "settle_ms", ctx, problems, cast=int, lo=0)
        return PlaceTowerEvent(eid, t, typ, comment, jitter, d.get("tower", ""), slot, p or Point(0, 0),
                               settle, _bool(d.get("confirm_click"), True, "confirm_click", ctx, problems),
                               _expect(d, ctx, problems))
    if typ == "upgrade":
        _no_unknown(d, _BASE_KEYS | {"target_pos", "upgrade_button_pos", "times", "between_ms", "expect"}, ctx, problems)
        tp = _point(d, "target_pos", ctx, problems)
        up = _point(d, "upgrade_button_pos", ctx, problems)
        times = _num(d.get("times"), 1, "times", ctx, problems, cast=int, lo=1, hi=50)
        between = _num(d.get("between_ms"), 300, "between_ms", ctx, problems, cast=int, lo=0)
        return UpgradeEvent(eid, t, typ, comment, jitter, tp or Point(0, 0), up or Point(0, 0),
                            times, between, _expect(d, ctx, problems))
    if typ == "ability":
        _no_unknown(d, _BASE_KEYS | {"tower_pos", "ability_button_pos", "confirm", "confirm_pos"}, ctx, problems)
        twp = _point(d, "tower_pos", ctx, problems)
        abp = _point(d, "ability_button_pos", ctx, problems)
        confirm = _bool(d.get("confirm"), False, "confirm", ctx, problems)
        # if confirm is requested, confirm_pos is required (else expand_macro silently
        # drops the confirm click and the ability never fires) — recheck #w2.1
        cp = _point(d, "confirm_pos", ctx, problems, required=confirm)
        return AbilityEvent(eid, t, typ, comment, jitter, twp or Point(0, 0), abp or Point(0, 0),
                            confirm, cp)
    problems.append(f"{ctx}: unknown event type {typ!r}")
    return None


# --------------------------------------------------------------------------- #
# recovery / run-end / header containers
# --------------------------------------------------------------------------- #
@dataclass
class DetectorSpec:
    ref_frame: str
    region: Rect
    threshold: float = 0.88
    action: str = "leave_and_restart"
    mask: list = field(default_factory=list)

    def to_dict(self):
        d = {"ref_frame": self.ref_frame, "region": self.region.to_dict(), "threshold": self.threshold}
        if self.action:
            d["action"] = self.action
        if self.mask:
            d["mask"] = [m.to_dict() for m in self.mask]
        return d


@dataclass
class RecoverySpec:
    wrong_map: Optional[DetectorSpec] = None
    disconnect: Optional[DetectorSpec] = None
    # proof we're back at the TDS hub/lobby, so recovery can VISUALLY CONFIRM a
    # leave/reset/reconnect succeeded instead of assuming it (plan R17 / §8.5).
    lobby_anchor: Optional[DetectorSpec] = None

    def to_dict(self):
        d = {}
        if self.wrong_map:
            d["wrong_map"] = self.wrong_map.to_dict()
        if self.disconnect:
            d["disconnect"] = self.disconnect.to_dict()
        if self.lobby_anchor:
            d["lobby_anchor"] = self.lobby_anchor.to_dict()
        return d


@dataclass
class RunEnd:
    victory: Optional[DetectorSpec] = None
    defeat: Optional[DetectorSpec] = None
    timeout_ms: int = 600000

    def to_dict(self):
        d = {"timeout_ms": self.timeout_ms}
        if self.victory:
            d["victory"] = self.victory.to_dict()
        if self.defeat:
            d["defeat"] = self.defeat.to_dict()
        return d


@dataclass
class Header:
    name: str = ""
    game: str = "Tower Defense Simulator"
    map: str = ""
    difficulty: str = ""
    mode: str = "solo"
    created: str = ""
    created_by: str = ""
    window_aspect: float = 0.0
    reference_resolution: dict = field(default_factory=dict)
    retina_scale_captured_at: float = 1.0
    private_server_url: str = ""  # join this server by opening the link (Feature A)
    notes: str = ""

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Header":
        if not isinstance(d, dict):
            return cls()
        h = cls()
        known = set(h.__dict__)
        for k, v in d.items():
            if k in known:
                setattr(h, k, v)
        # coerce numeric/dict fields so a malformed header never crashes a consumer
        # (engine._arm aspect check, calibrate's f"{...:.3f}")
        h.window_aspect = _safe_float(h.window_aspect, 0.0)
        h.retina_scale_captured_at = _safe_float(h.retina_scale_captured_at, 1.0)
        if not isinstance(h.reference_resolution, dict):
            h.reference_resolution = {}
        return h

    def recorded_logical_size(self) -> Optional[tuple[int, int]]:
        """The window size (in logical points) the strat was recorded at, or None if unknown.
        reference_resolution is stored in PHYSICAL pixels, so divide by the captured Retina scale to
        recover logical points — the space window geometry / clicks live in. Used to resize the live
        Roblox window back to the recorded size before play so screen-position clicks (tower placement)
        land on the same spot."""
        rr = self.reference_resolution or {}
        try:
            pw, ph = float(rr.get("w", 0)), float(rr.get("h", 0))
            ret = float(self.retina_scale_captured_at or 1.0)
        except (TypeError, ValueError):
            return None
        if pw > 0 and ph > 0 and ret > 0:
            return (round(pw / ret), round(ph / ret))
        return None


def _detector(d, name, ctx, problems) -> Optional[DetectorSpec]:
    if d is None:
        return None
    if not isinstance(d, dict):
        problems.append(f"{ctx}.{name}: must be an object")
        return None
    _no_unknown(d, {"ref_frame", "region", "threshold", "action", "mask"}, f"{ctx}.{name}", problems)
    rf = _req_str(d, "ref_frame", f"{ctx}.{name}", problems)
    region = _rect(d, "region", f"{ctx}.{name}", problems)
    action = _enum(d.get("action"), RECOVERY_ACTIONS, "action", f"{ctx}.{name}", problems,
                   default="leave_and_restart")
    return DetectorSpec(rf, region or Rect(0, 0, 1, 1),
                        _threshold(d.get("threshold"), 0.88, f"{ctx}.{name}", problems),
                        action, _mask(d, f"{ctx}.{name}", problems))


# --------------------------------------------------------------------------- #
# the strat file
# --------------------------------------------------------------------------- #
@dataclass
class StratFile:
    header: Header = field(default_factory=Header)
    config_overrides: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    join_sequence: list = field(default_factory=list)
    # Roblox-client-level leave/reset path (Esc -> Leave / Reset Character), recorded
    # by the user for their client version (plan section 8 two-layer recovery).
    leave_reset_sequence: list = field(default_factory=list)
    run_end: Optional[RunEnd] = None
    expected_map_check: Optional[DetectorSpec] = None
    recovery: RecoverySpec = field(default_factory=RecoverySpec)
    schema_version: int = SCHEMA_VERSION
    base_dir: str = ""  # not serialized; for resolving frame paths

    def to_dict(self) -> dict:
        d = {
            "schema_version": self.schema_version,
            "header": self.header.to_dict(),
            "config_overrides": self.config_overrides,
            "events": [e.to_dict() for e in self.events],
        }
        if self.join_sequence:
            d["join_sequence"] = [e.to_dict() for e in self.join_sequence]
        if self.leave_reset_sequence:
            d["leave_reset_sequence"] = [e.to_dict() for e in self.leave_reset_sequence]
        if self.run_end:
            d["run_end"] = self.run_end.to_dict()
        if self.expected_map_check:
            d["expected_map_check"] = self.expected_map_check.to_dict()
        if self.recovery.to_dict():
            d["recovery"] = self.recovery.to_dict()
        return d

    def resolve_frame(self, ref: str) -> str:
        if os.path.isabs(ref):
            return ref
        return os.path.join(self.base_dir, ref)


def _collect_frame_refs(strat: StratFile) -> list[tuple[str, str]]:
    """(label, ref_frame_path) for every reference image the strat names."""
    refs: list[tuple[str, str]] = []
    for seq in (strat.events, strat.join_sequence, strat.leave_reset_sequence):
        for e in seq:
            if isinstance(e, SyncPointEvent) and e.ref_frame:
                refs.append((f"sync:{e.label or e.id}", e.ref_frame))
            for attr in ("expect",):
                spec = getattr(e, attr, None)
                if spec and spec.ref_frame:
                    refs.append((f"{attr}:{e.id}", spec.ref_frame))
    for name in ("wrong_map", "disconnect", "lobby_anchor"):
        det = getattr(strat.recovery, name)
        if det and det.ref_frame:
            refs.append((f"recovery:{name}", det.ref_frame))
    if strat.expected_map_check and strat.expected_map_check.ref_frame:
        refs.append(("expected_map_check", strat.expected_map_check.ref_frame))
    if strat.run_end:
        for name in ("victory", "defeat"):
            det = getattr(strat.run_end, name)
            if det and det.ref_frame:
                refs.append((f"run_end:{name}", det.ref_frame))
    return refs


def parse(data: dict, base_dir: str = "", check_frames: bool = True) -> StratFile:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise StratValidationError(["top level must be a JSON object"])

    ver = data.get("schema_version", SCHEMA_VERSION)
    if not _is_num(ver) or (isinstance(ver, float) and not math.isfinite(ver)):
        problems.append("schema_version must be a finite integer")
        ver = SCHEMA_VERSION
    ver = int(ver)  # safe now: finite + numeric
    if ver > SCHEMA_VERSION:
        problems.append(f"schema_version {ver} is newer than supported ({SCHEMA_VERSION}); update the macro")
    data = migrate(data, ver)

    _no_unknown(data, {"schema_version", "header", "config_overrides", "events", "join_sequence",
                       "leave_reset_sequence", "run_end", "expected_map_check", "recovery"},
                "strat (top level)", problems)

    # A non-dict header is INTENTIONALLY tolerated (Header.from_dict defaults gracefully) so a malformed
    # metadata block never blocks playing — see test_bugfixes_round1; not changed (round 26 #6 declined).
    header = Header.from_dict(data.get("header", {}))
    if header.private_server_url and not looks_like_roblox_url(str(header.private_server_url)):
        problems.append("header.private_server_url must be a Roblox link "
                        "(https://...roblox.com... or roblox://...)")

    def _events(key) -> list:
        raw = data.get(key, []) or []
        if not isinstance(raw, list):
            problems.append(f"'{key}' must be a list")
            return []
        out = []
        for i, ed in enumerate(raw):
            ev = _build_event(ed, f"{key}[{i}]", problems)
            if ev:
                out.append(ev)
        return out

    events = _events("events")
    join_sequence = _events("join_sequence")
    leave_reset_sequence = _events("leave_reset_sequence")
    # recovery._run_sequence has no SyncPointEvent branch, so a sync_point here would be silently
    # dropped (its visual wait skipped) and later menu clicks fire blind (round 22c #12). Also covers
    # the macro->sync path: a place_tower/upgrade with an `expect` block expands to a sync (round 23 #2).
    for i, ev in enumerate(leave_reset_sequence):
        if any(p.type == "sync_point" for p in expand_macro(ev)):
            problems.append(f"leave_reset_sequence[{i}]: a sync_point (or a macro with 'expect') is not "
                            "supported here (recovery replays this sequence on a fixed timer, not the "
                            "visual engine)")

    rec = data.get("recovery")
    if rec is not None and not isinstance(rec, dict):
        problems.append("recovery must be an object")
    rec_raw = rec if isinstance(rec, dict) else {}
    # a typo'd key (e.g. "wrng_map") would silently drop a recovery detector
    _no_unknown(rec_raw, {"wrong_map", "disconnect", "lobby_anchor"}, "recovery", problems)
    recovery = RecoverySpec(
        _detector(rec_raw.get("wrong_map"), "wrong_map", "recovery", problems),
        _detector(rec_raw.get("disconnect"), "disconnect", "recovery", problems),
        _detector(rec_raw.get("lobby_anchor"), "lobby_anchor", "recovery", problems),
    )
    if (recovery.disconnect or recovery.wrong_map) and recovery.lobby_anchor is None:
        # without a lobby_anchor, recovery can't visually CONFIRM it reached the hub, so the
        # per-cause budget climbs to STOP even on a successful reconnect/reset (R17). Warn (not a
        # hard error — omitting it is allowed, just STOP-bounded) (round 23 #6).
        log.warning("recovery has a disconnect/wrong_map detector but no lobby_anchor; recovery "
                    "cannot confirm success and will STOP after max_attempts_per_cause attempts")
    # M17: disconnect action should reconnect, never reset_character
    if recovery.disconnect and recovery.disconnect.action == "reset_and_rejoin":
        problems.append("recovery.disconnect.action should be 'reconnect_and_rejoin' (no character "
                        "exists to reset while disconnected)")

    expected_map_check = _detector(data.get("expected_map_check"), "expected_map_check", "", problems)

    run_end = None
    if "run_end" in data:
        re = data["run_end"]
        if not isinstance(re, dict):
            problems.append("run_end must be an object")
        else:
            _no_unknown(re, {"victory", "defeat", "timeout_ms"}, "run_end", problems)
            # lo=1: a <=0 deadline makes _wait_run_end return immediately -> spurious STUCK_SYNC every
            # run (negative round 22b #1; ==0 round 22c #9). A None fallback would crash now+None, so
            # keep the 600000 default on reject.
            to = _num(re.get("timeout_ms"), 600000, "timeout_ms", "run_end", problems, cast=int, lo=1)
            run_end = RunEnd(
                _detector(re.get("victory"), "victory", "run_end", problems),
                _detector(re.get("defeat"), "defeat", "run_end", problems),
                to,
            )

    co = data.get("config_overrides")
    if co is not None and not isinstance(co, dict):
        problems.append("config_overrides must be an object")
    co = co if isinstance(co, dict) else {}

    strat = StratFile(header, co, events, join_sequence,
                      leave_reset_sequence, run_end, expected_map_check, recovery, int(ver), base_dir)

    if check_frames:
        for label, ref in _collect_frame_refs(strat):
            path = strat.resolve_frame(ref)
            if not os.path.exists(path):
                problems.append(f"{label}: reference frame not found: {ref} (resolved: {path})")
                continue
            try:
                w, h = png_dimensions(path)
                if w <= 0 or h <= 0:
                    problems.append(f"{label}: reference frame has invalid dimensions: {ref}")
            except Exception as e:
                problems.append(f"{label}: reference frame is not a valid PNG ({ref}): {e}")

    if problems:
        raise StratValidationError(problems)
    return strat


def migrate(data: dict, version: int) -> dict:
    """Apply migration shims for older schema versions (none yet)."""
    # Future: if version < N: transform data ...
    return data


def load(path: str, check_frames: bool = True) -> StratFile:
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise StratValidationError([f"JSON syntax error: {e}"], path=path)
        except UnicodeDecodeError as e:
            # a non-UTF-8 byte (e.g. a Latin-1 accent saved by a non-UTF-8 editor) must surface as a
            # clean validation error, not an uncaught traceback in the CLI (round 26 #5)
            raise StratValidationError([f"file is not valid UTF-8: {e}"], path=path)
    try:
        return parse(data, base_dir=os.path.dirname(os.path.abspath(path)), check_frames=check_frames)
    except StratValidationError as e:
        raise StratValidationError(e.problems, path=path)


def save(strat: StratFile, path: str) -> None:
    """Atomically write the strat to JSON (temp in same dir + fsync + os.replace, S11)."""
    text = json.dumps(strat.to_dict(), indent=2)
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --------------------------------------------------------------------------- #
# macro expansion (M8): primitives carry ABSOLUTE t_ms so internal waits rebase
# --------------------------------------------------------------------------- #
def _expect_sync(spec: ExpectSpec, base_id: int, t_ms: int) -> SyncPointEvent:
    return SyncPointEvent(base_id, t_ms, "sync_point", comment="auto: verify action took",
                          label=f"expect_{base_id}", ref_frame=spec.ref_frame, region=spec.region,
                          threshold=spec.threshold, timeout_ms=spec.timeout_ms, on_timeout="recover")


def expand_macro(ev: Event) -> list[Event]:
    """Expand a TDS macro into primitive events with absolute t_ms (plan M8).

    Primitive/sync events pass through unchanged.
    """
    if ev.type in PRIMITIVE_TYPES:
        return [ev]
    out: list[Event] = []
    t = ev.t_ms
    if isinstance(ev, PlaceTowerEvent):
        key = str(ev.hotbar_slot)
        out.append(KeyPressEvent(ev.id, t, "key_press", key=key, comment=f"arm {ev.tower}"))
        out.append(KeyReleaseEvent(ev.id, t + _KEY_GAP, "key_release", key=key))
        out.append(MouseMoveEvent(ev.id, t + _KEY_GAP + 10, "mouse_move", pos=ev.pos, duration_ms=_MOVE_DUR))
        last = t + _KEY_GAP + 10 + _MOVE_DUR + _POST_MOVE
        # The placement click is ALWAYS emitted: in TDS an armed tower is only placed BY
        # clicking, so a confirm_click=False path produced an unplaceable tower (recheck #w-place).
        out.append(ClickEvent(ev.id, last, "click", pos=ev.pos, comment=f"place {ev.tower}"))
        end = last + ev.settle_ms
        if ev.expect:
            out.append(_expect_sync(ev.expect, ev.id, end))
        else:
            out.append(WaitEvent(ev.id, end, "wait", duration_ms=0, reason="settle"))
        return out
    if isinstance(ev, UpgradeEvent):
        out.append(ClickEvent(ev.id, t, "click", pos=ev.target_pos, comment="open upgrade panel"))
        for i in range(max(1, ev.times)):
            out.append(ClickEvent(ev.id, t + _PANEL_GAP + i * ev.between_ms, "click",
                                  pos=ev.upgrade_button_pos, comment=f"upgrade {i+1}"))
        end = t + _PANEL_GAP + max(0, ev.times - 1) * ev.between_ms
        if ev.expect:
            out.append(_expect_sync(ev.expect, ev.id, end + 50))
        return out
    if isinstance(ev, AbilityEvent):
        out.append(ClickEvent(ev.id, t, "click", pos=ev.tower_pos, comment="select tower"))
        out.append(ClickEvent(ev.id, t + _KEY_GAP, "click", pos=ev.ability_button_pos, comment="fire ability"))
        if ev.confirm and ev.confirm_pos is not None:
            out.append(ClickEvent(ev.id, t + 2 * _KEY_GAP, "click", pos=ev.confirm_pos, comment="confirm ability target"))
        return out
    return [ev]


def expand_all(events: list[Event]) -> list[Event]:
    out: list[Event] = []
    for ev in events:
        prims = expand_macro(ev)
        if ev.jitter_ms is not None:  # a macro's per-event jitter (incl. explicit 0) must reach its
            for p in prims:  # expanded primitives, else it round-trips with zero effect (round 22 #G / 22c #7)
                if p is not ev:  # primitive passthrough already carries its own jitter_ms
                    p.jitter_ms = ev.jitter_ms
        out.extend(prims)
    out.sort(key=lambda e: e.t_ms)
    return out
