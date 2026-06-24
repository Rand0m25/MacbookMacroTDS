"""Player / replay engine + top-level RunLoop FSM.

Timing model (single authority, plan M8): every primitive carries an absolute
``t_ms``; the engine sleeps until ``iter_t0 + t_ms + clock_offset`` then dispatches.
``clock_offset`` is rebased at each sync barrier and is MONOTONIC / stretch-only
(plan M1): a sync can only push later events LATER, never pull them earlier, so
lag never collapses the inter-event spacing humanization relies on.

The adaptive sync barrier (the headline feature) polls a small live ROI and
fires when it matches the recorded reference, with stability debouncing,
timeout decoupled from the stability window (plan M2), an optional rising-edge /
settle gate (S2), and panic-aware sleeps (M10). On timeout it routes to the
injected RecoveryController (plan M13) so this file stays policy-free and
testable with mocks.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .config import Config, MatchMethod
from .errors import PanicAbort
from .frame import Frame
from .geometry import Coordinates, Point, WindowGeometry
from .recovery import FailureMode, Outcome
from .strat import (
    AbilityEvent, ClickEvent, DragEvent, Event, KeyPressEvent, KeyReleaseEvent,
    MouseMoveEvent, ScrollEvent, StratFile, SyncPointEvent, WaitEvent, expand_all,
)

log = logging.getLogger("tds_macro.engine")

_SETTLE_THRESHOLD = 0.97  # frame-to-frame similarity meaning "motion stopped" (S2)
_PARK = Point(0.995, 0.02)  # neutral cursor corner for sync polling (S3)


class RunState(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    LOBBY = "lobby"
    IN_MATCH = "in_match"
    WAIT_RUN_END = "wait_run_end"
    POSTMATCH = "postmatch"
    RECOVERY = "recovery"
    PAUSED = "paused"
    STOPPING = "stopping"
    PANIC = "panic"


class WaitResult(str, Enum):
    FIRE = "fire"
    TIMEOUT = "timeout"


@dataclass
class RunStats:
    runs: int = 0          # matches actually completed (reached run-end)
    restarts: int = 0      # loop iterations abandoned + restarted by recovery
    wins: int = 0
    losses: int = 0
    recoveries: int = 0
    sync_timeouts: int = 0
    stopped_reason: str = ""


class _RestartLoop(Exception):
    """Internal: abandon the current iteration and restart the farm loop."""


class _StopRun(Exception):
    """Internal: halt the whole run (recovery exhausted / fatal)."""


def _default_ref_loader(path: str) -> Frame:
    from .visual import load_reference

    return load_reference(path)


class Player:
    def __init__(self, strat: StratFile, window, input_backend, capture, comparator,
                 clock, recovery, config: Config, hotkeys=None,
                 ref_loader: Optional[Callable[[str], Frame]] = None) -> None:
        self.strat = strat
        self.window = window
        self.input = input_backend
        self.capture = capture
        self.comparator = comparator
        self.clock = clock
        self.recovery = recovery
        self.config = config
        self.hotkeys = hotkeys
        self.ref_loader = ref_loader or _default_ref_loader
        self.stats = RunStats()
        self.state = RunState.IDLE

        self._geo: WindowGeometry = window.get_geometry()
        self._coords = Coordinates(self._geo)
        self._iter_t0 = 0.0
        self.clock_offset = 0.0
        self._last_guard_ms = -1e18
        self._ref_cache: dict[str, Frame] = {}
        self._rng = random.Random(0xC0FFEE)

    # --- helpers ----------------------------------------------------------
    def _should_abort(self) -> bool:
        return bool(self.hotkeys and self.hotkeys.should_abort())

    def _abort_check(self) -> None:
        if self._should_abort():
            raise PanicAbort("panic/stop requested")

    def _absorb_wall_time(self, since_ms: float) -> None:
        """Add off-timeline wall time (pause, recovery) to the monotonic clock
        offset so it never collapses the spacing of the remaining events (M1/D7)."""
        consumed = self.clock.now_ms() - since_ms
        if consumed > 0:
            self.clock_offset += consumed

    def _maybe_pause(self) -> None:
        if not self.hotkeys:
            return
        if not self.hotkeys.events.pause.is_set():
            return
        start = self.clock.now_ms()
        while self.hotkeys.events.pause.is_set() and not self.hotkeys.should_abort():
            self.state = RunState.PAUSED
            self.clock.sleep(50)
        self._absorb_wall_time(start)  # don't let the pause make later events fire back-to-back

    def _elapsed(self) -> float:
        return self.clock.now_ms() - self._iter_t0

    def _refresh_geo(self) -> None:
        self._geo = self.window.get_geometry()
        self._coords = Coordinates(self._geo)

    def _ref(self, path: str, label: str) -> Frame:
        key = self.strat.resolve_frame(path)
        f = self._ref_cache.get(key)
        if f is None:
            f = self.ref_loader(key)
            self._ref_cache[key] = f
        f.label = label
        return f

    def _logical(self, p: Point) -> tuple[float, float]:
        if self.config.click_offset_px and self.config.input_backend.value != "mock":
            jx = self._rng.uniform(-1, 1) * self.config.click_offset_px / max(1, self._geo.w)
            jy = self._rng.uniform(-1, 1) * self.config.click_offset_px / max(1, self._geo.h)
            p = Point(p.x + jx, p.y + jy)
        return self._coords.norm_to_logical(p)

    # --- dispatch ---------------------------------------------------------
    def _dispatch_primitive(self, e: Event) -> None:
        if self.config.dry_run:
            log.debug("dry-run skip %s id=%s", e.type, e.id)
            return
        sa = self._should_abort
        if isinstance(e, MouseMoveEvent):
            px, py = self._logical(e.pos)
            self.input.move(px, py, e.duration_ms, self.config.mouse_move_hz, self.clock, sa)
        elif isinstance(e, ClickEvent):
            if e.pos is not None:
                px, py = self._logical(e.pos)
            else:
                px = py = None
            hold = e.hold_ms or self.config.default_click_hold_ms
            self.input.click(e.button, px, py, e.clicks, hold)
        elif isinstance(e, DragEvent):
            fx, fy = self._logical(e.frm)
            tx, ty = self._logical(e.to)
            self.input.drag(e.button, fx, fy, tx, ty, e.duration_ms, self.config.mouse_move_hz, self.clock, sa)
        elif isinstance(e, KeyPressEvent):
            self.input.press_key(e.key, e.modifiers)
        elif isinstance(e, KeyReleaseEvent):
            self.input.release_key(e.key, e.modifiers)
        elif isinstance(e, ScrollEvent):
            if e.pos is not None:
                px, py = self._logical(e.pos)
                self.input.move(px, py)
            self.input.scroll(e.dx, e.dy)
        elif isinstance(e, WaitEvent):
            pass  # the scheduled sleep already realized the delay
        else:
            log.warning("unknown primitive at dispatch: %s", e.type)

    # --- the adaptive visual-sync barrier --------------------------------
    def _adaptive_wait(self, sync: SyncPointEvent) -> tuple[WaitResult, Optional[Frame]]:
        cfg = self.config
        threshold = sync.threshold if sync.threshold is not None else cfg.sync_default_threshold
        method = sync.match or cfg.sync_match_method
        poll = max(1, sync.poll_ms or cfg.sync_poll_ms)  # never 0 -> no busy-spin (R6 belt-and-suspenders)
        stability = sync.stability_frames or cfg.sync_stability_frames
        timeout = sync.timeout_ms or cfg.sync_default_timeout_ms
        # M2: never let the stability window outlast the timeout (warn, don't silently bump).
        min_timeout = (stability + 1) * poll + cfg.sync_timeout_slack_ms
        if timeout < min_timeout:
            log.warning("sync '%s' timeout_ms=%d is too small for stability_frames=%d @ poll=%dms; "
                        "raising to %d (M2)", sync.label, timeout, stability, poll, min_timeout)
            timeout = min_timeout

        ref = self._ref(sync.ref_frame, sync.label or sync.ref_frame)
        mask = sync.mask or None
        require_edge = sync.require_settled  # opt-in rising-edge/settle gate (S2)

        start = self.clock.now_ms()
        deadline = start + timeout
        streak = 0
        seen_low = not require_edge
        last_live: Optional[Frame] = None

        if cfg.sync_park_cursor and not str(sync.label).startswith("expect_") and not cfg.dry_run:
            try:
                px, py = self._coords.norm_to_logical(_PARK)
                self.input.move(px, py)
            except Exception:
                pass

        while True:
            self._abort_check()
            live = self.capture.grab_region(self._geo, sync.region)
            score = self.comparator.score(live, ref, method, mask)
            matched = score >= threshold
            if not matched:
                seen_low = True
            settled = True
            if require_edge and last_live is not None:
                settled = self.comparator.score(live, last_live, method) >= _SETTLE_THRESHOLD
            last_live = live
            log.debug("sync %s score=%.3f thr=%.3f streak=%d", sync.label, score, threshold, streak)

            if matched and seen_low and settled:
                streak += 1
                if streak >= stability:
                    return (WaitResult.FIRE, live)
            else:
                streak = 0

            # M2: timeout check AFTER the match check, so a match on the final poll wins.
            if self.clock.now_ms() >= deadline:
                return (WaitResult.TIMEOUT, live)
            self.clock.sleep(poll)
            self._abort_check()

    def _rebase_clock(self, sync_t_ms: int) -> None:
        """Monotonic, stretch-only rebase (plan M1)."""
        candidate = self._elapsed() - sync_t_ms
        if candidate > self.clock_offset:
            self.clock_offset = candidate
            log.debug("clock stretched: offset=%.0fms", self.clock_offset)

    # --- guards (cheap, throttled) ---------------------------------------
    def _maybe_run_guards(self) -> None:
        now = self.clock.now_ms()
        if now - self._last_guard_ms < self.config.recovery_check_every_ms:
            return
        self._last_guard_ms = now
        self._refresh_geo()
        fm = self._detect_failure()
        if fm != FailureMode.NONE:
            self._route_recovery(fm)

    def _detect_failure(self) -> FailureMode:
        if not self.window.is_frontmost():
            return FailureMode.FOCUS_LOST
        window = None
        rec = self.strat.recovery
        for det, mode in ((rec.disconnect, FailureMode.DISCONNECTED),
                          (rec.wrong_map, FailureMode.WRONG_MAP)):
            if det is None:
                continue
            window = window or self.capture.grab_window(self._geo)
            ref = self._ref(det.ref_frame, mode.value)
            if self.comparator.score(window, ref, self.config.sync_match_method, det.mask or None) >= det.threshold:
                return mode
        return FailureMode.NONE

    def _route_recovery(self, fm: FailureMode) -> None:
        self.state = RunState.RECOVERY
        self.stats.recoveries += 1
        started = self.clock.now_ms()
        scene = self.capture.grab_window(self._geo)
        outcome = self.recovery.handle(fm, scene=scene)
        if outcome == Outcome.STOP:
            raise _StopRun(f"recovery stopped on {fm.value}")
        if outcome == Outcome.REJOIN:
            raise _RestartLoop(fm.value)
        # RESUME: absorb the wall time recovery consumed so the rest of the
        # timeline keeps its spacing, then continue where we were (D7).
        self._absorb_wall_time(started)
        self.state = RunState.IN_MATCH

    # --- sequence playback ------------------------------------------------
    def _play_sequence(self, events: list[Event], state: RunState) -> None:
        self.state = state
        # join_sequence and events are independently recorded, each with its own
        # absolute t_ms starting near 0. Rebase the timeline to "now" at the start
        # of each so the second sequence doesn't fire back-to-back (D3 / M1).
        self._iter_t0 = self.clock.now_ms()
        self.clock_offset = 0.0
        prims = expand_all(events)
        for e in prims:
            self._abort_check()
            self._maybe_pause()
            jitter = 0
            if self.config.jitter_ms:
                jitter = self._rng.uniform(-self.config.jitter_ms, self.config.jitter_ms)
            target = self._iter_t0 + e.t_ms + self.clock_offset + jitter
            self.clock.sleep_until(target)
            self._maybe_run_guards()

            if isinstance(e, SyncPointEvent):
                attempts = 0
                while True:
                    result, _window = self._adaptive_wait(e)
                    if result == WaitResult.FIRE:
                        self._rebase_clock(e.t_ms)
                        break
                    self.stats.sync_timeouts += 1
                    if e.on_timeout == "retry" and attempts < self.config.sync_max_retries:
                        attempts += 1
                        self._rebase_clock(e.t_ms)  # keep downstream spacing while we retry
                        log.info("sync '%s' retry %d/%d", e.label, attempts, self.config.sync_max_retries)
                        continue
                    self._handle_sync_timeout(e)  # escalate (abort/continue/recover)
                    break
                continue
            self._dispatch_primitive(e)

    def _handle_sync_timeout(self, sync: SyncPointEvent) -> None:
        action = sync.on_timeout
        log.info("sync '%s' timed out -> %s", sync.label, action)
        if action == "continue":
            self._rebase_clock(sync.t_ms)  # absorb the elapsed timeout so later events keep spacing
            return
        if action == "abort":
            raise _StopRun(f"sync '{sync.label}' timed out (on_timeout=abort)")
        # "recover", or "retry" whose retry budget is now exhausted -> classify + recover
        if str(sync.label).startswith("expect_"):
            # an action-verify sync that never confirmed: the action didn't take
            fm = FailureMode.OUT_OF_CASH
        else:
            self._refresh_geo()
            full = self.capture.grab_window(self._geo)  # classify on the FULL frame (D9), not the ROI
            fm = self.recovery.classify(full)
            if fm == FailureMode.NONE:
                fm = FailureMode.STUCK_SYNC
        self._route_recovery(fm)

    # --- run-end detection (M15) -----------------------------------------
    def _wait_run_end(self) -> FailureMode:
        re = self.strat.run_end
        if re is None:
            return FailureMode.NONE
        self.state = RunState.WAIT_RUN_END
        deadline = self.clock.now_ms() + re.timeout_ms
        while self.clock.now_ms() < deadline:
            self._abort_check()
            self._refresh_geo()
            window = self.capture.grab_window(self._geo)
            for det, mode in ((re.victory, FailureMode.VICTORY), (re.defeat, FailureMode.DEFEAT)):
                if det is None:
                    continue
                ref = self._ref(det.ref_frame, mode.value)
                if self.comparator.score(window, ref, self.config.sync_match_method, det.mask or None) >= det.threshold:
                    return mode
            # also catch disconnect/kick while waiting for the end
            fm = self._detect_failure()
            if fm in (FailureMode.DISCONNECTED, FailureMode.WRONG_MAP):
                return fm
            self.clock.sleep(self.config.recovery_check_every_ms)
        return FailureMode.NONE

    # --- top-level run loop ----------------------------------------------
    def _arm(self) -> None:
        self.state = RunState.ARMING
        self._refresh_geo()
        from .geometry import aspect_mismatch

        ha = self.strat.header.window_aspect
        if ha and aspect_mismatch(self._geo, ha, self.config.aspect_warn_tolerance):
            msg = (f"live window aspect {self._geo.aspect:.3f} differs from recorded {ha:.3f}; "
                   "UI-button taps may miss. Match the recorded window size + Roblox GUI scale.")
            if self.config.block_on_aspect_mismatch:
                raise _StopRun(msg)
            log.warning(msg)

    def _verify_expected_map(self) -> None:
        det = self.strat.expected_map_check
        if det is None:
            return
        self._refresh_geo()
        window = self.capture.grab_window(self._geo)
        ref = self._ref(det.ref_frame, "expected_map")
        score = self.comparator.score(window, ref, self.config.sync_match_method, det.mask or None)
        if score < det.threshold:
            log.warning("expected-map check failed (score=%.3f < %.3f) -> recovery WRONG_MAP", score, det.threshold)
            self._route_recovery(FailureMode.WRONG_MAP)

    def run(self) -> RunStats:
        try:
            self._arm()
            loop_count = self.config.loop_count
            consecutive_restarts = 0
            while True:
                self._abort_check()  # panic/stop always interrupts the loop (D-r3)
                iter_start = self.clock.now_ms()
                try:
                    self._iter_t0 = self.clock.now_ms()
                    self.clock_offset = 0.0
                    self._last_guard_ms = -1e18

                    if self.strat.join_sequence:
                        self._play_sequence(self.strat.join_sequence, RunState.LOBBY)
                    self._verify_expected_map()
                    self._play_sequence(self.strat.events, RunState.IN_MATCH)

                    end = self._wait_run_end()
                    self.state = RunState.POSTMATCH
                    if end == FailureMode.VICTORY:
                        self.stats.wins += 1
                    elif end == FailureMode.DEFEAT:
                        self.stats.losses += 1
                    elif end in (FailureMode.DISCONNECTED, FailureMode.WRONG_MAP):
                        self._route_recovery(end)  # may raise _RestartLoop/_StopRun

                    self.stats.runs += 1
                    consecutive_restarts = 0  # a run actually completed
                except _RestartLoop as r:
                    log.info("restarting loop after: %s", r)
                    self.stats.restarts += 1  # NOT a completed run (R6: don't satisfy loop_count)
                    consecutive_restarts += 1
                    try:
                        self.input.release_all()  # don't carry held input into the restart (R6)
                    except Exception:
                        pass
                    if consecutive_restarts >= self.config.max_consecutive_restarts:
                        self.stats.stopped_reason = (
                            f"aborted after {consecutive_restarts} consecutive restarts without a "
                            "completed run")
                        break

                if loop_count and self.stats.runs >= loop_count:
                    self.stats.stopped_reason = "loop_count reached"
                    break
                self._maybe_break_between_runs()
                # never busy-spin a zero-work iteration (e.g. empty events + no
                # run_end); the floor sleep is panic-aware on RealClock (D-r3).
                idle = self.config.min_inter_event_ms - (self.clock.now_ms() - iter_start)
                if idle > 0:
                    self.clock.sleep(idle)
        except PanicAbort:
            self.state = RunState.PANIC
            self.stats.stopped_reason = "panic"
        except _StopRun as s:
            self.state = RunState.STOPPING
            self.stats.stopped_reason = str(s)
        except Exception as e:  # noqa: BLE001 - never crash an input-automation loop ungracefully
            self.state = RunState.STOPPING
            self.stats.stopped_reason = f"error: {type(e).__name__}: {e}"
            log.exception("unexpected error in run loop")
        finally:
            try:
                self.input.release_all()
            except Exception:
                pass
            try:
                self.capture.close()
            except Exception:
                pass
        return self.stats

    def _maybe_break_between_runs(self) -> None:
        cfg = self.config
        if cfg.break_every_runs and cfg.break_seconds and self.stats.runs % cfg.break_every_runs == 0:
            log.info("taking a %ss break after %d runs (humanization)", cfg.break_seconds, self.stats.runs)
            self.clock.sleep(cfg.break_seconds * 1000)
