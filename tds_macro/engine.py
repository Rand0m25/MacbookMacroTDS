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
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .config import Config
from .errors import PanicAbort, WindowNotFoundError
from .frame import Frame
from .geometry import Coordinates, Point, WindowGeometry
from .recovery import FailureMode, Outcome
from .strat import (
    ClickEvent, DragEvent, Event, KeyPressEvent, KeyReleaseEvent,
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


_BENIGN_ZERO_RUN_REASONS = ("", "panic", "session cap reached")  # not failures even with 0 completed matches


@dataclass
class RunStats:
    runs: int = 0          # matches actually completed (reached run-end)
    restarts: int = 0      # loop iterations abandoned + restarted by recovery
    wins: int = 0
    losses: int = 0
    recoveries: int = 0
    sync_timeouts: int = 0
    stopped_reason: str = ""

    def is_failure(self) -> bool:
        """A real failure: it completed ZERO matches AND didn't stop for a benign reason — a user
        panic/Stop, the configured session cap, or a clean (empty) stop. So a cold-start launch that
        never got a window ('error: ...'), recovery giving up ('recovery stopped on ...'), or the
        restart budget running out ('aborted after N consecutive restarts ...') all count as failures,
        while a finished/loop_count/panic/session-cap stop does not. Drives the play exit code and the
        GUI's failure surfacing so both agree (review round 24 #B)."""
        if self.runs > 0:
            return False
        return (self.stopped_reason or "") not in _BENIGN_ZERO_RUN_REASONS


class _RestartLoop(Exception):
    """Internal: abandon the current iteration and restart the farm loop."""


class _StopRun(Exception):
    """Internal: halt the whole run (recovery exhausted / fatal)."""


class _RunComplete(Exception):
    """Internal: a stuck sync reclassified to VICTORY/DEFEAT -> the match actually finished; the loop
    must credit it as a win/loss completed run, NOT a restart (round 23 #3). Carries the mode."""

    def __init__(self, mode):
        super().__init__(mode.value)
        self.mode = mode


def _default_ref_loader(path: str) -> Frame:
    from .visual import load_reference

    return load_reference(path)


def _default_launcher(config: Config):
    from .launcher import make_launcher

    return make_launcher(config)


class Player:
    def __init__(self, strat: StratFile, window, input_backend, capture, comparator,
                 clock, recovery, config: Config, hotkeys=None,
                 ref_loader: Optional[Callable[[str], Frame]] = None, launcher=None) -> None:
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
        self.launcher = launcher or _default_launcher(config)
        self.stats = RunStats()
        self.state = RunState.IDLE

        self._private_server_opened = False  # open the private-server link once per session, not every loop
        # Acquire the window geometry up front. If Roblox isn't running yet we normally fail fast here —
        # BUT if a private-server link is configured (and this isn't a dry-run preview) we DEFER: run()
        # will open the link to LAUNCH Roblox, then acquire geometry once its window appears. With no link
        # there's nothing to launch, so keep the original fail-fast contract (CLI/GUI "could not start").
        try:
            self._geo: Optional[WindowGeometry] = window.get_geometry()
            self._coords: Optional[Coordinates] = Coordinates(self._geo)
        except WindowNotFoundError:
            # ONLY a genuinely-absent window is deferrable. Catching every Exception would also swallow
            # an ImportError / transient Quartz fault / a bad Coordinates() into a needless launch + a
            # launch_timeout_ms hang — those are real bugs that should fail fast (review round 24 #3).
            if self.config.dry_run or not self._private_server_url():
                raise
            self._geo = None  # deferred; run() -> _ensure_window_or_launch() acquires it after launching
            self._coords = None
        self._iter_t0 = 0.0
        self.clock_offset = 0.0
        self._last_guard_ms = -1e18
        self._ref_cache: dict[str, Frame] = {}
        self._rng = random.Random(0xC0FFEE)

    # input-bearing primitives whose effect depends on Roblox being the focused window; a WaitEvent
    # sends nothing, so it needs no foreground gate, and a SyncPointEvent has its own branch.
    _INPUT_EVENTS = (MouseMoveEvent, ClickEvent, DragEvent, KeyPressEvent, KeyReleaseEvent, ScrollEvent)

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
        prev = self.state  # restore after resume so observers don't see stale PAUSED (recheck #w-pause)
        start = self.clock.now_ms()
        while self.hotkeys.events.pause.is_set() and not self.hotkeys.should_abort():
            self.state = RunState.PAUSED
            self.clock.sleep(50)
        self._absorb_wall_time(start)  # don't let the pause make later events fire back-to-back
        self.state = prev

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
            self.input.move(px, py, e.duration_ms, self.config.mouse_move_hz, self.clock, sa, e.easing)
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

    def _foreground_ok_for_input(self, e: Event) -> bool:
        """Validate Roblox is the frontmost window right before we dispatch an input primitive, so a
        click/keypress can never land in another app. Unlike _maybe_run_guards this is NOT throttled —
        it runs on every input event. On focus loss it routes the FOCUS_LOST recovery (activate + settle,
        budget -> STOP); if focus still isn't ours afterward the caller skips the primitive instead of
        firing blind. Dry-run and verify_foreground=False bypass it (no input is sent / opted out)."""
        if self.config.dry_run or not self.config.verify_foreground:
            return True
        if not isinstance(e, self._INPUT_EVENTS):
            return True  # WaitEvent etc. send nothing
        if self.window.is_frontmost():
            return True
        log.warning("Roblox not frontmost before %s id=%s -> recovering focus before sending input",
                    e.type, e.id)
        self._route_recovery(FailureMode.FOCUS_LOST)  # RESUME on refocus, else raises _StopRun
        return self.window.is_frontmost()

    # --- the adaptive visual-sync barrier --------------------------------
    def _adaptive_wait(self, sync: SyncPointEvent) -> tuple[WaitResult, Optional[Frame]]:
        cfg = self.config
        threshold = sync.threshold if sync.threshold is not None else cfg.sync_default_threshold
        method = sync.match or cfg.sync_match_method
        poll = max(1, sync.poll_ms or cfg.sync_poll_ms)  # never 0 -> no busy-spin (R6 belt-and-suspenders)
        stability = max(1, sync.stability_frames or cfg.sync_stability_frames)  # negative would bypass debounce (round 22 #F)
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
            elif last_live is None:
                seen_low = True  # already in target state at entry -> no falling edge needed (recheck #w2.2)
            settled = True
            if require_edge:
                # require at least one frame-to-frame settle comparison before firing, so
                # require_settled actually gates even at stability_frames==1 (recheck #w-settle).
                # Use the SAME mask as the match, else a masked-out dynamic region (timer/anim)
                # keeps frame-to-frame similarity below threshold forever (recheck #w-settle-mask).
                settled = (last_live is not None
                           and self.comparator.score(live, last_live, method, mask) >= _SETTLE_THRESHOLD)
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
        prev = self.state  # restore the caller's phase on RESUME (LOBBY/WAIT_RUN_END/IN_MATCH) (recheck #w-routestate)
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
        # timeline keeps its spacing, then continue in the phase we came from (D7).
        self._absorb_wall_time(started)
        self.state = prev if prev != RunState.RECOVERY else RunState.IN_MATCH

    # --- sync-point localization (opt-in: find which checkpoint we're at, by matching ALL sync frames) ---
    def _reanchor_to(self, sync_t_ms: int) -> None:
        """Re-anchor the timeline so the event at ``sync_t_ms`` fires NOW. Unlike the monotonic
        stretch-only _rebase_clock, this is a deliberate DISCONTINUITY — used only when localization
        jumps execution to a different checkpoint. Callers must also reset prev_target to None."""
        self._iter_t0 = self.clock.now_ms() - sync_t_ms
        self.clock_offset = 0.0

    def _localize_against_syncs(self, prims, sync_idxs, *, expected_i, live):
        """Score the live screen against ALL sync frames and return the index of the single best sync to
        jump to, or None to decline. A candidate must clear max(localize_min_score, its OWN threshold) —
        so the localizer is never laxer than the barrier it bypasses — and the winner must beat the
        2nd-best by localize_margin (ambiguous -> decline). Skips action-verify ('expect_') syncs and the
        expected sync itself; honors localize_allow_rewind (no backward jump by default). Identical
        regions are grabbed once (the user's full-screen frames would otherwise be 20 identical grabs)."""
        self._refresh_geo()
        cache: dict = {}

        def grab(region):
            key = (region.x, region.y, region.w, region.h)
            if key not in cache:
                cache[key] = self.capture.grab_region(self._geo, region)
            return cache[key]

        scored = []
        for k in sync_idxs:
            if k == expected_i:  # "jumping" to where we already are would just re-time-out -> loop
                continue
            s = prims[k]
            if str(s.label).startswith("expect_"):  # action-verify syncs aren't checkpoints
                continue
            if expected_i is not None and k < expected_i and not self.config.localize_allow_rewind:
                continue  # forward-only by default (a rewind re-runs intervening clicks)
            try:
                # reuse the barrier's live grab for candidates sharing the timed-out sync's region
                if live is not None and expected_i is not None and s.region == prims[expected_i].region:
                    frame = live
                else:
                    frame = grab(s.region)
                ref = self._ref(s.ref_frame, s.label or s.ref_frame)
                score = self.comparator.score(frame, ref, s.match or self.config.sync_match_method, s.mask or None)
            except Exception as ex:  # a missing/unreadable frame must not crash playback
                log.debug("localize: scoring sync %r failed: %s", s.label, ex)
                continue
            floor = max(self.config.localize_min_score,
                        s.threshold if s.threshold is not None else self.config.sync_default_threshold)
            if score >= floor:
                scored.append((score, k))
        if not scored:
            return None
        scored.sort(reverse=True)
        if len(scored) >= 2 and scored[0][0] - scored[1][0] < self.config.localize_margin:
            log.info("localize: ambiguous (%.3f vs %.3f) -> declining", scored[0][0], scored[1][0])
            return None
        return scored[0][1]

    # --- sequence playback ------------------------------------------------
    def _play_sequence(self, events: list[Event], state: RunState, *, localize: bool = False) -> None:
        self.state = state
        # join_sequence and events are independently recorded, each with its own
        # absolute t_ms starting near 0. Rebase the timeline to "now" at the start
        # of each so the second sequence doesn't fire back-to-back (D3 / M1).
        self._iter_t0 = self.clock.now_ms()
        self.clock_offset = 0.0
        prims = expand_all(events)
        sync_idxs = [k for k, e in enumerate(prims) if isinstance(e, SyncPointEvent)]
        prev_target = None
        i = 0
        resync_jumps = 0

        # Hook A — start-time localization: jump to the checkpoint the live screen is actually at, so a
        # resume mid-run starts in the right place instead of replaying from the top. Declining keeps i=0,
        # so a normal fresh start still plays the (un-anchored) opening.
        if (localize and self.config.localize_on_start and sync_idxs and not self.config.dry_run):
            scan_start = self.clock.now_ms()
            j = self._localize_against_syncs(prims, sync_idxs, expected_i=None, live=None)
            if j is not None:
                i = j  # land ON the matched sync so its full barrier re-confirms before any input fires
                self._reanchor_to(prims[j].t_ms)
                log.info("localize: starting at sync %r (index %d)", prims[j].label, j)
            else:
                # the scan itself burned wall time; absorb it so the opening events keep their recorded
                # spacing instead of firing in a back-to-back burst at loop start (round 26 #2)
                self._absorb_wall_time(scan_start)

        while i < len(prims):
            e = prims[i]
            self._abort_check()
            self._maybe_pause()
            jit = e.jitter_ms if e.jitter_ms is not None else self.config.jitter_ms  # explicit 0 suppresses (round 22c #7)
            jitter = self._rng.uniform(-jit, jit) if jit else 0
            target = self._iter_t0 + e.t_ms + self.clock_offset + jitter
            if prev_target is not None and target < prev_target:
                # jitter must not pull an event before the previous one: a negative draw could
                # otherwise make sleep_until return instantly and fire two events back-to-back,
                # collapsing/inverting their intended spacing (round 21 #1).
                target = prev_target
            self.clock.sleep_until(target)
            prev_target = target
            self._maybe_run_guards()

            if isinstance(e, SyncPointEvent):
                jumped = False
                attempts = 0
                focus_retries = 0
                while True:
                    result, live = self._adaptive_wait(e)
                    if result == WaitResult.FIRE:
                        self._rebase_clock(e.t_ms)
                        break
                    self.stats.sync_timeouts += 1
                    if e.on_timeout == "retry" and attempts < self.config.sync_max_retries:
                        attempts += 1
                        self._rebase_clock(e.t_ms)  # keep downstream spacing while we retry
                        log.info("sync '%s' retry %d/%d", e.label, attempts, self.config.sync_max_retries)
                        continue
                    # Hook B — resync-on-timeout: rather than recover, check ALL syncs and JUMP to the one
                    # the screen actually matches. Skip action-verify ('expect_') syncs (let their
                    # OUT_OF_CASH path stand) and respect the jump budget. Gated off in dry-run (mirrors
                    # Hook A) so a preview replays linearly.
                    if (localize and self.config.localize_on_timeout and not self.config.dry_run
                            and not str(e.label).startswith("expect_")
                            and resync_jumps < self.config.localize_max_jumps):
                        j = self._localize_against_syncs(prims, sync_idxs, expected_i=i, live=live)
                        if j is not None:
                            resync_jumps += 1
                            # the jump skips events i+1..j-1; if a key was pressed before here and its
                            # release lies in that skipped range it would stay physically held, so drain
                            # held input first — mirrors the release_all() on the restart/run-complete paths.
                            try:
                                self.input.release_all()
                            except Exception:
                                pass
                            i = j  # land ON the matched sync (its barrier re-confirms)
                            self._reanchor_to(prims[j].t_ms)
                            prev_target = None
                            jumped = True
                            log.info("localize: resynced %r -> %r after timeout", e.label, prims[j].label)
                            break
                    if self._handle_sync_timeout(e):  # focus-only repair -> re-confirm, don't skip the gate
                        if focus_retries < self.config.sync_max_retries:
                            focus_retries += 1
                            continue  # re-run the barrier now that focus is back
                        # focus kept flapping -> treat as a genuinely stuck sync (restart), never advance blind
                        self._route_recovery(FailureMode.STUCK_SYNC)  # raises _RestartLoop/_StopRun
                    break
                if jumped:
                    continue  # i already advanced to the jump target; don't increment past it
                i += 1
                continue
            if not self._foreground_ok_for_input(e):
                log.warning("skipping %s id=%s: Roblox still not frontmost after recovery", e.type, e.id)
                i += 1
                continue
            self._dispatch_primitive(e)
            i += 1

    def _handle_sync_timeout(self, sync: SyncPointEvent) -> bool:
        """Escalate a timed-out sync. Returns True ONLY when the timeout was repaired by a focus-only
        RESUME (the visual checkpoint was never confirmed) so the caller re-runs the barrier instead of
        advancing past it; every other outcome either returns False (advance, e.g. on_timeout='continue')
        or raises (_StopRun / _RestartLoop / _RunComplete)."""
        action = sync.on_timeout
        log.info("sync '%s' timed out -> %s", sync.label, action)
        if action == "continue":
            self._rebase_clock(sync.t_ms)  # absorb the elapsed timeout so later events keep spacing
            return False
        if action == "abort":
            raise _StopRun(f"sync '{sync.label}' timed out (on_timeout=abort)")
        # "recover" / retry-exhausted: absorb the elapsed timeout (mirror 'continue') so a
        # RESUME recovery outcome doesn't collapse the remaining timeline's spacing (recheck #6).
        self._rebase_clock(sync.t_ms)
        if str(sync.label).startswith("expect_"):
            # an action-verify sync that never confirmed: the action didn't take
            fm = FailureMode.OUT_OF_CASH
        else:
            self._refresh_geo()
            full = self.capture.grab_window(self._geo)  # classify on the FULL frame (D9), not the ROI
            fm = self.recovery.classify(full)
            if fm in (FailureMode.VICTORY, FailureMode.DEFEAT) and self.state == RunState.IN_MATCH:
                # the match actually ended (win/loss screen is up) -> credit it as a completed run,
                # don't route through recovery/_RestartLoop and burn the restart budget (round 23 #3).
                # Gate on IN_MATCH so a stale end-screen during LOBBY/join nav doesn't credit a
                # phantom run with zero gameplay (round 23 #4/#7).
                raise _RunComplete(fm)
            if fm == FailureMode.NONE:
                fm = FailureMode.STUCK_SYNC
        self._route_recovery(fm)  # raises for REJOIN/STOP; returns only on a FOCUS_LOST RESUME
        # A focus-only repair is the ONE non-raising outcome: the game state behind the barrier was
        # never actually confirmed, so signal the caller to re-run the barrier rather than fire the
        # next input blind on an unconfirmed checkpoint (round 26 #1).
        return fm == FailureMode.FOCUS_LOST

    # --- run-end detection (M15) -----------------------------------------
    def _match_run_end(self, window) -> Optional[FailureMode]:
        re = self.strat.run_end
        for det, mode in ((re.victory, FailureMode.VICTORY), (re.defeat, FailureMode.DEFEAT)):
            if det is None:
                continue
            ref = self._ref(det.ref_frame, mode.value)
            if self.comparator.score(window, ref, self.config.sync_match_method, det.mask or None) >= det.threshold:
                return mode
        return None

    def _wait_run_end(self) -> FailureMode:
        re = self.strat.run_end
        if re is None:
            return FailureMode.NONE
        self.state = RunState.WAIT_RUN_END
        deadline = self.clock.now_ms() + re.timeout_ms
        added = 0.0  # cumulative refocus extension, capped so focus-flapping can't hang us
        while self.clock.now_ms() < deadline:
            self._abort_check()
            self._refresh_geo()
            window = self.capture.grab_window(self._geo)
            mode = self._match_run_end(window)
            if mode is not None:
                return mode
            # also catch focus-loss / disconnect / kick while waiting for the end
            fm = self._detect_failure()
            if fm == FailureMode.FOCUS_LOST:
                # _detect_failure returns FOCUS_LOST first, which would otherwise mask
                # disconnect/wrong-map; re-acquire focus then keep waiting (recheck #w1).
                t0 = self.clock.now_ms()
                self._route_recovery(fm)  # RESUME on refocus, else raises _StopRun
                # extend by the refocus time so it doesn't eat the run-end window (recheck #w11),
                # but cap the TOTAL extension at one timeout_ms so persistent focus-flapping
                # can't grow the deadline without bound and hang the loop (recheck #w-flap)
                delta = self.clock.now_ms() - t0
                if added < re.timeout_ms:
                    deadline += min(delta, re.timeout_ms - added)
                    added += delta
            elif fm in (FailureMode.DISCONNECTED, FailureMode.WRONG_MAP):
                return fm
            self.clock.sleep(max(1, self.config.recovery_check_every_ms))  # never 0 -> no hang/busy-spin
        # Final check: the win/loss screen may have appeared in the gap between the last poll and the
        # deadline. Don't return NONE on a real end (that routes a genuine win/loss to spurious
        # STUCK_SYNC recovery and counts it as a restart) (round 23 #6).
        self._refresh_geo()
        mode = self._match_run_end(self.capture.grab_window(self._geo))
        return mode if mode is not None else FailureMode.NONE

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

    def _center_cursor_in_window(self) -> None:
        """Move the cursor into the MIDDLE of the Roblox window before playback begins, so the first
        recorded action starts from inside the game. A cursor left in another app or on a second monitor
        can otherwise make an opening hover / relative move land outside Roblox. Best-effort and gated:
        no-op in dry-run, when disabled, or before geometry is known (any failure is swallowed — it must
        never block a run)."""
        if self.config.dry_run or not self.config.center_cursor_on_play or self._coords is None:
            return
        try:
            px, py = self._coords.norm_to_logical(Point(0.5, 0.5))
            self.input.move(px, py)
        except Exception:  # noqa: BLE001 - centering is a convenience, never fatal
            log.debug("center-cursor-on-play skipped", exc_info=True)

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

    # --- join (private-server link or recorded lobby clicks) -------------
    def _private_server_url(self) -> str:
        return (self.config.private_server_url or self.strat.header.private_server_url or "").strip()

    def _await_join(self) -> bool:
        """After opening the private-server link, wait (up to join_timeout_ms) for Roblox
        to be frontmost and — if an expected_map_check is configured — the map to appear.
        Returns False on timeout. Works for both auto-launch and (browser auto-open) flows."""
        det = self.strat.expected_map_check
        deadline = self.clock.now_ms() + self.config.join_timeout_ms
        added = 0.0  # cap pause-extension so pause-flapping can't hang the join (mirrors _wait_run_end)
        while self.clock.now_ms() < deadline:
            self._abort_check()
            t0 = self.clock.now_ms()
            self._maybe_pause()
            # a pause longer than join_timeout_ms must not eat the join window and trigger a
            # spurious WRONG_MAP on resume; extend the deadline by the paused time (round 19 #1).
            delta = self.clock.now_ms() - t0
            if delta and added < self.config.join_timeout_ms:
                deadline += min(delta, self.config.join_timeout_ms - added)
                added += delta
            try:
                self.window.activate()  # nudge Roblox to the foreground (the browser may have it)
            except Exception:
                pass
            if self.window.is_frontmost():
                if det is None:
                    return True  # nothing to confirm against -> focused == joined
                self._refresh_geo()
                frame = self.capture.grab_window(self._geo)
                ref = self._ref(det.ref_frame, "expected_map")
                if self.comparator.score(frame, ref, self.config.sync_match_method,
                                         det.mask or None) >= det.threshold:
                    return True
            self.clock.sleep(max(1, self.config.recovery_check_every_ms))
        log.warning("join: timed out (%dms) waiting for the private server to load", self.config.join_timeout_ms)
        return False

    def _join(self) -> None:
        """One join path used at loop start AND on every recovery rejoin (design #4):
        open the private-server link ONCE per session (you stay in the same server across
        matches — re-opening it every loop would reload the whole server and pop the
        "Open Roblox?" handler each time), wait for it to load, then play join_sequence
        (full lobby nav when there's no link, or post-load re-queue clicks when there is).
        A real disconnect is handled by recovery, which re-opens the link on its own path.
        A link that never loads on the first open routes to bounded WRONG_MAP recovery."""
        url = self._private_server_url()
        if url and not self.config.dry_run and not self._private_server_opened:  # dry-run must not open/stall (recheck #w-dry)
            log.info("join: opening private server link (once per session)")
            self.launcher.open_url(url)
            self._private_server_opened = True  # never re-open in the main loop; recovery owns any rejoin
            if not self._await_join():
                self._route_recovery(FailureMode.WRONG_MAP)  # bounded by max_consecutive_restarts
        if self.strat.join_sequence:
            self._play_sequence(self.strat.join_sequence, RunState.LOBBY)

    def _ensure_window_or_launch(self) -> None:
        """If no Roblox window was found at construction (``_geo is None``) but a private-server link is
        configured, OPEN the link to LAUNCH Roblox into that server and wait (up to ``launch_timeout_ms``)
        for its window to appear — so 'Play' works from a cold start instead of failing 'window not found'.
        No-op when the window is already up, or when there's no link / it's a dry-run (``_arm()`` then
        surfaces the missing window exactly as before)."""
        if self._geo is not None:
            return  # __init__ already acquired geometry — Roblox is running
        url = self._private_server_url()
        if not url or self.config.dry_run:
            return  # nothing to launch with (or dry-run preview) -> let _arm() raise as before
        self.state = RunState.ARMING  # surface "launching" in the GUI status line while we wait
        log.info("no Roblox window detected; opening the private server link to launch it")
        self.launcher.open_url(url)
        self._private_server_opened = True  # launched here -> _join() must not re-open it this session
        deadline = self.clock.now_ms() + self.config.launch_timeout_ms
        while self._geo is None:
            self._abort_check()  # let panic/stop interrupt a long cold launch
            try:
                self._refresh_geo()  # window up yet? on success this sets _geo/_coords and ends the loop
            except WindowNotFoundError:  # only "not up yet" is retriable; any other error must surface
                if self.clock.now_ms() >= deadline:
                    log.warning("launched the private server but no Roblox window appeared within %dms",
                                self.config.launch_timeout_ms)
                    return  # _arm()/_refresh_geo() will raise a clear WindowNotFoundError -> graceful stop
                self.clock.sleep(max(1, self.config.recovery_check_every_ms))
        log.info("Roblox window detected after launch; waiting for the server to load")
        self._await_join()  # best-effort: front + (expected map); _verify_expected_map() re-checks in-loop

    def run(self) -> RunStats:
        try:
            self._ensure_window_or_launch()  # cold start: launch Roblox via the private-server link if needed
            self._arm()
            self._center_cursor_in_window()  # start with the cursor inside the Roblox window (before any play)
            loop_count = self.config.loop_count
            session_start = self.clock.now_ms()  # for the session_max_minutes cap (round 22 #H)
            consecutive_restarts = 0
            while True:
                self._abort_check()  # panic/stop always interrupts the loop (D-r3)
                iter_start = self.clock.now_ms()
                completed = False
                try:
                    self._iter_t0 = self.clock.now_ms()
                    self.clock_offset = 0.0
                    self._last_guard_ms = -1e18

                    self._join()  # private-server link and/or recorded lobby clicks
                    self._verify_expected_map()
                    # localize only the in-match timeline (join_sequence + recovery replay stay linear)
                    self._play_sequence(self.strat.events, RunState.IN_MATCH, localize=True)

                    end = self._wait_run_end()
                    self.state = RunState.POSTMATCH
                    if end == FailureMode.VICTORY:
                        self.stats.wins += 1
                    elif end == FailureMode.DEFEAT:
                        self.stats.losses += 1
                    elif end in (FailureMode.DISCONNECTED, FailureMode.WRONG_MAP):
                        self._route_recovery(end)  # may raise _RestartLoop/_StopRun
                    elif end == FailureMode.NONE and self.strat.run_end is not None:
                        # run_end was configured but neither victory/defeat/disconnect/
                        # wrong-map confirmed before timeout_ms -> the match is stuck, not
                        # finished. Route to recovery (leave/reset/rejoin) instead of
                        # silently counting a phantom run that would satisfy loop_count and
                        # reset the restart budget, violating the line-61 invariant
                        # "runs = matches actually completed (reached run-end)" (round 17 #2).
                        # STUCK_SYNC -> REJOIN raises _RestartLoop (bounded by
                        # max_consecutive_restarts); over-budget -> _StopRun.
                        self._route_recovery(FailureMode.STUCK_SYNC)

                    self.stats.runs += 1
                    consecutive_restarts = 0  # a run actually completed
                    completed = True
                except _RunComplete as rc:
                    # a stuck sync that was really the win/loss screen: count it like the normal
                    # _wait_run_end VICTORY/DEFEAT branch, not as a restart (round 23 #3)
                    try:
                        self.input.release_all()  # the abandoned mid-sequence sync may hold input (round 23 #5)
                    except Exception:
                        pass
                    self.state = RunState.POSTMATCH
                    if rc.mode == FailureMode.VICTORY:
                        self.stats.wins += 1
                    else:
                        self.stats.losses += 1
                    self.stats.runs += 1
                    consecutive_restarts = 0
                    completed = True
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
                if (self.config.session_max_minutes > 0 and self.clock.now_ms() - session_start
                        >= self.config.session_max_minutes * 60_000):
                    # documented anti-detection/session cap (was never enforced) — checked between
                    # iterations, so it can't interrupt an in-progress match (round 22 #H)
                    self.stats.stopped_reason = "session cap reached"
                    break
                if completed:  # only break between COMPLETED runs, not restart attempts (recheck #w-break)
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
        if (cfg.break_every_runs and cfg.break_seconds and self.stats.runs > 0
                and self.stats.runs % cfg.break_every_runs == 0):
            log.info("taking a %ss break after %d runs (humanization)", cfg.break_seconds, self.stats.runs)
            self.clock.sleep(cfg.break_seconds * 1000)
