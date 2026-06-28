"""Typed runtime configuration.

Pure stdlib dataclass (no Pydantic) so the core has zero third-party deps and is
fully testable on the Linux dev box. Values come from defaults, then a strat's
``config_overrides`` block, then CLI flags (highest priority).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from enum import Enum
from urllib.parse import urlparse


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


def looks_like_roblox_url(url: str) -> bool:
    """True if the string is plausibly a Roblox link we can `open` to join a server
    (a roblox:// deep link or an https roblox.com / ro.blox.com share link)."""
    u = (url or "").strip().lower()
    if u.startswith("roblox://") or u.startswith("roblox-player:"):
        return True
    if u.startswith("http://") or u.startswith("https://"):
        if "\\" in u:
            # browsers normalize backslashes to slashes (WHATWG) but urlparse doesn't, so
            # 'https://evil.com\\@roblox.com' parses as host roblox.com here yet opens evil.com -> reject (round 23 #8)
            return False
        # Parse the host instead of a bare substring test: "roblox.com" in u would also accept
        # roblox.com.evil.example / evilroblox.com / ?ref=roblox.com and then hand that URL to the
        # OS `open`, letting a typo'd or hostile link steer the join (round 21 #3).
        try:
            host = urlparse(u).hostname or ""
        except ValueError:
            return False  # malformed host (e.g. 'http://[::1') -> not a valid link, not a crash (round 22 #A)
        return (host in ("roblox.com", "ro.blox.com")
                or host.endswith(".roblox.com") or host.endswith(".ro.blox.com"))
    return False


# Fields that are declared Optional and may legitimately be set back to None via an override.
# Every other field is non-nullable (None would crash validate()/playback).
_NULLABLE_FIELDS = frozenset({"retina_scale_override", "window_rect_override"})


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
    # (the ban-risk consent gate is enforced unconditionally in cli._check_consent via the consent
    #  file + --accept-ban-risk; it is intentionally NOT a config knob a strat could disable.)

    # --- hotkeys ---
    panic_hotkey: str = "f8"
    start_hotkey: str = "f9"
    pause_hotkey: str = "f7"
    mark_sync_hotkey: str = "f10"
    killswitch_file: str = ""  # optional last-resort panic file (R21 nice-to-have)

    # --- window / display ---
    window_title_match: str = "Roblox"
    relaunch_url: str = ""  # roblox://... or https experience URL for RELAUNCH_EXPERIENCE fallback
    private_server_url: str = ""  # roblox private-server link; join by opening it (preferred over join_sequence)
    join_timeout_ms: int = 30000  # how long to wait for the server to load after opening the link
    launch_timeout_ms: int = 60000  # if Roblox isn't running, how long to wait for its window to appear
    #                                 after opening the private-server link to LAUNCH it (cold start is slow)

    # --- sync-point localization (opt-in; default OFF -> playback is byte-identical) ---
    # Instead of trusting the fixed timeline, score the live screen against ALL sync frames to find
    # which checkpoint we're actually at, and resume there. Only useful when sync regions are SMALL and
    # DISTINCT — full-screen frames of the same map are non-discriminative and the localizer will decline.
    localize_on_start: bool = False    # at play start, jump to the matching checkpoint (resume mid-run)
    localize_on_timeout: bool = False  # on a sync timeout, jump to a matching checkpoint instead of recovering
    localize_min_score: float = 0.85   # a candidate must clear max(this, the sync's own threshold)
    localize_margin: float = 0.05      # best must beat 2nd-best by this; ambiguous -> decline (no jump)
    localize_allow_rewind: bool = False  # forward-only by default (a rewind re-spends cash / re-places towers)
    localize_max_jumps: int = 20       # cap resync jumps per sequence so it can't thrash
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
    verify_foreground: bool = True  # validate Roblox is frontmost right before every input primitive,
    #                                 so a click/keypress can never land in another app (else focus is
    #                                 only checked every recovery_check_every_ms and input fires blind)
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
            if isinstance(value, bool):  # bool is an int subclass; reject it like every other numeric field
                raise ValueError("retina_scale_override must be a number")
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
            # Key off the field's declared nullability, not the CURRENT value: a nullable field
            # that already holds a value must still be clearable back to None (round 20 #1).
            # JSON null on any other field would crash validate()/playback -> reject it.
            if key in _NULLABLE_FIELDS:
                return None
            raise ValueError(f"{key} must not be null")
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
            if isinstance(value, bool):
                # bool is an int subclass and the bool-branch above keys off the CURRENT value
                # (an int), so True/False would slip through as 1/0 — reject the typo (round 22 #C).
                raise ValueError(f"{key} must be an integer, got {value!r}")
            if isinstance(value, int):  # exact, skip the float round-trip
                return value
            try:
                f = float(value)  # accepts "100"; "12.5"/"abc"/1.9 are rejected clearly below
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer")
            if not math.isfinite(f):
                raise ValueError(f"{key} must be a finite integer")
            if not f.is_integer():  # don't silently truncate 1.9 -> 1 (round 17 #1)
                raise ValueError(f"{key} must be an integer, got {value!r}")
            return int(f)
        if isinstance(current, float):
            if isinstance(value, bool):  # bool is a number subclass; reject like the int branch (round 22c #1)
                raise ValueError(f"{key} must be a number, got {value!r}")
            f = float(value)
            if not math.isfinite(f):
                raise ValueError(f"{key} must be a finite number")
            return f
        if isinstance(current, str) and not isinstance(value, str):
            return str(value)  # str field given a non-string -> coerce (don't crash str consumers)
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
        if self.recovery_check_every_ms <= 0:
            problems.append("recovery_check_every_ms must be > 0")  # else _wait_run_end hangs/busy-spins
        if self.retina_scale_override is not None and self.retina_scale_override <= 0:
            problems.append("retina_scale_override must be > 0")
        if self.window_rect_override is not None and (self.window_rect_override[2] <= 0
                                                      or self.window_rect_override[3] <= 0):
            problems.append("window_rect_override width and height must be > 0")  # else degenerate geometry (round 22c #2)
        if self.jitter_ms < 0:
            problems.append("jitter_ms must be >= 0")  # round 22c #8
        if self.private_server_url and not looks_like_roblox_url(self.private_server_url):
            problems.append("private_server_url must be a Roblox link (https://...roblox.com... or roblox://...)")
        if self.relaunch_url and not looks_like_roblox_url(self.relaunch_url):
            # same OS-`open` gate as private_server_url — it's the recovery relaunch fallback (round 22 #B)
            problems.append("relaunch_url must be a Roblox link (https://...roblox.com... or roblox://...)")
        if any(c in self.window_title_match for c in '"\\\n\r'):
            # window_title_match is interpolated into an osascript program in window.activate();
            # quotes/backslashes/newlines could inject AppleScript from an untrusted strat (round 22 #L)
            problems.append("window_title_match must not contain quotes, backslashes, or newlines")
        if self.join_timeout_ms <= 0:
            problems.append("join_timeout_ms must be > 0")
        if self.launch_timeout_ms <= 0:
            problems.append("launch_timeout_ms must be > 0")  # else the launch-wait loop busy-spins/can't time out
        if not (0.0 <= self.localize_min_score <= 1.0):
            problems.append("localize_min_score must be in [0, 1]")
        if self.localize_margin < 0:
            problems.append("localize_margin must be >= 0")
        if self.localize_max_jumps < 0:
            problems.append("localize_max_jumps must be >= 0")
        # Reject an incoherent real/mock mix (e.g. a strat's config_overrides flipping window_backend to
        # "mock" on a real run): a mock window reports frontmost=True with fake 1600x900 geometry, which
        # neutralizes the focus guard and fires REAL clicks at arbitrary screen points (round 26 #7).
        if ((self.window_backend == WindowBackendKind.MOCK or self.screen_backend == ScreenBackendKind.MOCK)
                and self.input_backend != InputBackendKind.MOCK):
            problems.append("incoherent backends: a mock window/screen backend with a real input backend "
                            "would fire real input against fake geometry")
        return problems

    def min_sync_timeout_ms(self) -> int:
        """Smallest timeout that still allows a stability streak to complete (M2)."""
        return (self.sync_stability_frames + 1) * self.sync_poll_ms + self.sync_timeout_slack_ms
