"""Typed runtime configuration.

Pure stdlib dataclass (no Pydantic) so the core has zero third-party deps and is
fully testable on the Linux dev box. Values come from defaults, then a strat's
``config_overrides`` block, then CLI flags (highest priority).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from enum import Enum


class InputBackendKind(str, Enum):
    PYNPUT = "pynput"
    MOCK = "mock"


class ScreenBackendKind(str, Enum):
    MSS = "mss"
    MOCK = "mock"


class WindowBackendKind(str, Enum):
    QUARTZ = "quartz"
    MOCK = "mock"


class MatchMethod(str, Enum):
    TM_CCOEFF_NORMED = "tm_ccoeff_normed"  # brightness-tolerant cross-correlation
    TM_SQDIFF_NORMED = "tm_sqdiff_normed"
    NCC = "ncc"
    SSIM = "ssim"
    MSE = "mse"
    PHASH = "phash"


@dataclass
class Config:
    # --- paths ---
    strat_path: str = ""
    frames_dir: str = "frames"

    # --- loop / farming ---
    loop_count: int = 0  # 0 == infinite until panic/error

    # --- visual-sync defaults (overridable per sync_point) ---
    sync_default_threshold: float = 0.90
    sync_default_timeout_ms: int = 8000
    sync_poll_ms: int = 120
    sync_match_method: MatchMethod = MatchMethod.TM_CCOEFF_NORMED
    sync_stability_frames: int = 2
    sync_timeout_slack_ms: int = 500  # added to (stability+1)*poll min-timeout (M2)
    sync_max_retries: int = 2  # bounded re-polls of the barrier when on_timeout="retry"
    sync_park_cursor: bool = True  # park cursor out of ROI before polling (S3)

    # --- recovery ---
    recovery_threshold: float = 0.88
    recovery_check_every_ms: int = 1500
    max_attempts_per_cause: int = 3  # per-cause cap (M5)
    max_consecutive_restarts: int = 10  # bail if runs never complete (R6: loop_count counts completions)
    no_state_change_watchdog_ms: int = 60000

    # --- safety / timing ---
    action_timeout_ms: int = 15000
    mouse_move_hz: int = 120
    default_click_hold_ms: int = 25
    double_click_ms: int = 250
    jitter_ms: int = 0  # +/- random timing jitter at playback (humanization)
    click_offset_px: int = 0  # +/- random per-click pixel offset (humanization)
    min_inter_event_ms: int = 8  # floor between injected events (R10)

    # --- humanization / session caps ---
    session_max_minutes: int = 0  # 0 == no cap
    break_every_runs: int = 0  # 0 == no breaks
    break_seconds: int = 0
    require_consent: bool = True  # first-run ban-risk acknowledgement gate (R18)

    # --- hotkeys ---
    panic_hotkey: str = "f8"
    start_hotkey: str = "f9"
    pause_hotkey: str = "f7"
    mark_sync_hotkey: str = "f10"
    killswitch_file: str = ""  # optional last-resort panic file (R21 nice-to-have)

    # --- window / display ---
    window_title_match: str = "Roblox"
    relaunch_url: str = ""  # roblox://... or https experience URL for RELAUNCH_EXPERIENCE fallback
    retina_scale_override: float | None = None
    window_rect_override: tuple[int, int, int, int] | None = None  # (x,y,w,h) for mock/tests
    aspect_warn_tolerance: float = 0.02
    block_on_aspect_mismatch: bool = False  # S15: hard-block UI-critical strats

    # --- backends ---
    input_backend: InputBackendKind = InputBackendKind.PYNPUT
    screen_backend: ScreenBackendKind = ScreenBackendKind.MSS
    window_backend: WindowBackendKind = WindowBackendKind.QUARTZ

    # --- behaviour flags ---
    failsafe_corner: bool = True
    dry_run: bool = False
    log_level: str = "INFO"

    def with_overrides(self, overrides: dict) -> "Config":
        """Return a copy with recognised keys overridden (unknown keys ignored)."""
        valid = {f.name for f in fields(self)}
        kwargs = {k: v for k, v in self.__dict__.items()}
        for k, v in (overrides or {}).items():
            if k not in valid:
                continue
            cv = self._coerce(k, v)               # enums + special fields
            kwargs[k] = self._coerce_type(k, cv, getattr(self, k))  # numeric/bool fields (R6)
        return Config(**kwargs)

    @staticmethod
    def _coerce(key: str, value):
        if key == "sync_match_method" and not isinstance(value, MatchMethod):
            return MatchMethod(value)
        if key == "input_backend" and not isinstance(value, InputBackendKind):
            return InputBackendKind(value)
        if key == "screen_backend" and not isinstance(value, ScreenBackendKind):
            return ScreenBackendKind(value)
        if key == "window_backend" and not isinstance(value, WindowBackendKind):
            return WindowBackendKind(value)
        if key == "window_rect_override" and value is not None:
            if (isinstance(value, str) or not isinstance(value, (list, tuple)) or len(value) != 4
                    or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)):
                raise ValueError("window_rect_override must be a list of 4 numbers [x, y, w, h]")
            return tuple(int(x) for x in value)
        if key == "retina_scale_override" and value is not None:
            f = float(value)  # raises ValueError/TypeError on bad input (caught by CLI)
            if not math.isfinite(f):
                raise ValueError("retina_scale_override must be a finite number")
            return f
        return value

    @staticmethod
    def _coerce_type(key: str, value, current):
        """Coerce an override to the field's declared scalar type so a string like
        '100' becomes 100 (and is rejected if non-convertible) instead of crashing
        deep in playback arithmetic (R6)."""
        if value is None:
            return value
        if isinstance(current, bool):  # bool BEFORE int (bool is an int subclass)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                s = value.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off"):
                    return False
                raise ValueError(f"{key} must be a boolean")
            return bool(value)
        if isinstance(current, int):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{key} must be a finite integer")
            return int(value)
        if isinstance(current, float):
            f = float(value)
            if not math.isfinite(f):
                raise ValueError(f"{key} must be a finite number")
            return f
        return value

    def validate(self) -> list[str]:
        """Return a list of config problems (empty == ok)."""
        problems: list[str] = []
        if not (0.0 <= self.sync_default_threshold <= 1.0):
            problems.append("sync_default_threshold must be in 0..1")
        if self.sync_poll_ms <= 0:
            problems.append("sync_poll_ms must be > 0")
        if self.sync_stability_frames < 1:
            problems.append("sync_stability_frames must be >= 1")
        if self.loop_count < 0:
            problems.append("loop_count must be >= 0")
        if self.max_attempts_per_cause < 1:
            problems.append("max_attempts_per_cause must be >= 1")
        return problems

    def min_sync_timeout_ms(self) -> int:
        """Smallest timeout that still allows a stability streak to complete (M2)."""
        return (self.sync_stability_frames + 1) * self.sync_poll_ms + self.sync_timeout_slack_ms
