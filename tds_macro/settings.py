"""Persisted GUI settings.

A curated, user-editable subset of :class:`~tds_macro.config.Config` fields, edited in the GUI's
Settings window, stored as JSON at ``~/.tds_macro_settings.json`` and applied as config overrides on
every launch (``defaults -> these settings -> a strat's own config_overrides``). Tk-free so it can be
unit-tested without a display; the GUI view only reads :data:`GROUPS` to lay out widgets.
"""

from __future__ import annotations

import json
import os
import tempfile

from .config import Config

SETTINGS_PATH = os.path.expanduser("~/.tds_macro_settings.json")

# (group label, [(field, kind, label)]) — `kind` drives the widget + value parsing in the view.
GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Hotkeys", [
        ("panic_hotkey", "str", "Panic / stop"),
        ("start_hotkey", "str", "Start / resume"),
        ("pause_hotkey", "str", "Pause toggle"),
        ("mark_sync_hotkey", "str", "Mark sync (while recording)"),
    ]),
    ("Humanization & timing", [
        ("jitter_ms", "int", "Timing jitter (±ms)"),
        ("click_offset_px", "int", "Click offset (±px)"),
        ("default_click_hold_ms", "int", "Click hold (ms)"),
        ("mouse_move_hz", "int", "Mouse-move rate (Hz)"),
    ]),
    ("Sync localization", [
        ("localize_on_start", "bool", "Localize on start"),
        ("localize_on_timeout", "bool", "Localize on sync timeout"),
        ("localize_min_score", "float", "Min match score (0–1)"),
        ("localize_margin", "float", "Ambiguity margin"),
        ("localize_allow_rewind", "bool", "Allow backward jumps"),
        ("localize_max_jumps", "int", "Max resync jumps"),
    ]),
    ("Recovery, sync & safety", [
        ("verify_foreground", "bool", "Verify Roblox frontmost"),
        ("failsafe_corner", "bool", "Failsafe-corner abort"),
        ("block_on_aspect_mismatch", "bool", "Block on aspect mismatch"),
        ("sync_park_cursor", "bool", "Park cursor during sync"),
        ("center_cursor_on_play", "bool", "Center cursor before play"),
        ("sync_default_threshold", "float", "Sync match threshold (0–1)"),
        ("sync_default_timeout_ms", "int", "Sync timeout (ms)"),
        ("sync_poll_ms", "int", "Sync poll (ms)"),
        ("max_attempts_per_cause", "int", "Recovery attempts / cause"),
        ("max_consecutive_restarts", "int", "Max consecutive restarts"),
        ("recovery_check_every_ms", "int", "Guard-check interval (ms)"),
        ("join_timeout_ms", "int", "Join timeout (ms)"),
        ("launch_timeout_ms", "int", "Launch timeout (ms)"),
        ("session_max_minutes", "int", "Session cap (min, 0=off)"),
        ("break_every_runs", "int", "Break every N runs (0=off)"),
        ("break_seconds", "int", "Break length (s)"),
        ("window_title_match", "str", "Window title match"),
    ]),
]

# the flat list of editable fields (and a kind lookup) derived from GROUPS
FIELDS: list[str] = [f for _, items in GROUPS for (f, _kind, _label) in items]
KIND: dict[str, str] = {f: kind for _, items in GROUPS for (f, kind, _label) in items}


def defaults() -> dict:
    """The default value of every editable field, from a fresh Config."""
    c = Config()
    return {f: getattr(c, f) for f in FIELDS}


def load(path: str = SETTINGS_PATH) -> dict:
    """Read saved settings, keeping only known fields. Any problem (missing file, bad JSON, not an
    object) yields ``{}`` so a corrupt/absent file never blocks launching."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in FIELDS}


def save(settings: dict, path: str = SETTINGS_PATH) -> None:
    """Atomically write the known-field subset to JSON (temp in same dir + fsync + os.replace)."""
    clean = {k: settings[k] for k in FIELDS if k in settings}
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def validate(settings: dict) -> list[str]:
    """Coerce + bound-check the settings by applying them to a Config. Collects per-field coercion
    errors (so a bad value names its field), then runs Config.validate() for cross-field bounds.
    Returns a list of problems (empty == valid)."""
    problems: list[str] = []
    coerced: dict = {}
    base = Config()
    for k in FIELDS:
        if k not in settings:
            continue
        try:
            base.with_overrides({k: settings[k]})  # coerce this one field in isolation
            coerced[k] = settings[k]
        except (ValueError, TypeError) as e:
            problems.append(f"{k}: {e}")
    if problems:
        return problems
    return Config().with_overrides(coerced).validate()
