"""Error-recovery: failure modes, the controller Protocol, and a mock.

The real :class:`RecoveryController` (full FSM, plan M4/M5/M17) is defined later
in this file. The Protocol + enums + mock live up top so the engine can depend
only on the interface and tests can inject :class:`MockRecoveryController`
(plan M13) — engine/adaptive-sync tests then need no real recovery logic.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Optional, Protocol

from .frame import Frame

log = logging.getLogger("tds_macro.recovery")


class FailureMode(str, Enum):
    NONE = "none"
    WRONG_MAP = "wrong_map"
    DISCONNECTED = "disconnected"
    KICKED = "kicked"
    DEFEAT = "defeat"
    VICTORY = "victory"
    STUCK_SYNC = "stuck_sync"
    OUT_OF_CASH = "out_of_cash"
    STATE_MISMATCH = "state_mismatch"
    FOCUS_LOST = "focus_lost"


class Outcome(str, Enum):
    RESUME = "resume"      # recovered in place; engine continues prior state
    REJOIN = "rejoin"      # restart the loop iteration via join_sequence
    STOP = "stop"          # unrecoverable / budget exhausted -> halt + notify


class RecoveryControllerProtocol(Protocol):
    # the interface the engine depends on; the concrete RecoveryController (below) and
    # MockRecoveryController both satisfy it. (Renamed so it isn't shadowed by the concrete
    # class of the same name — recheck #w-redef.)
    def classify(self, window: Frame) -> FailureMode: ...
    def handle(self, reason, *, scene: Optional[Frame] = None) -> Outcome: ...


class MockRecoveryController:
    """Scripted recovery for tests."""

    def __init__(
        self,
        handle_fn: Optional[Callable[[object, Optional[Frame]], Outcome]] = None,
        classify_fn: Optional[Callable[[Frame], FailureMode]] = None,
    ) -> None:
        self.handle_fn = handle_fn
        self.classify_fn = classify_fn
        self.handle_calls: list[object] = []
        self.classify_calls: list[Frame] = []

    def classify(self, window: Frame) -> FailureMode:
        self.classify_calls.append(window)
        if self.classify_fn:
            return self.classify_fn(window)
        return FailureMode.NONE

    def handle(self, reason, *, scene: Optional[Frame] = None) -> Outcome:
        self.handle_calls.append(reason)
        if self.handle_fn:
            return self.handle_fn(reason, scene)
        return Outcome.REJOIN


# --------------------------------------------------------------------------- #
# Real recovery FSM (plan M4 transition table, M5 per-cause caps, M17 vocab).
#
#   FailureMode      -> action                              -> Outcome
#   FOCUS_LOST       -> refocus window                      -> RESUME / STOP
#   DISCONNECTED     -> reconnect (NOT reset; M17)          -> REJOIN
#   KICKED           -> already in lobby                    -> REJOIN
#   WRONG_MAP        -> leave + reset character (Roblox)    -> REJOIN
#   STUCK_SYNC       -> reclassify; else leave + reset      -> REJOIN
#   OUT_OF_CASH      -> leave                               -> REJOIN
#   STATE_MISMATCH   -> reconcile to screen                 -> REJOIN
#   DEFEAT/VICTORY   -> not recovery (loop handles)         -> REJOIN
#   any cause over max_attempts_per_cause                   -> STOP + notify
# --------------------------------------------------------------------------- #
class RecoveryController:
    """The user-mandated leave/reset->lobby->rejoin machine, fully bounded."""

    def __init__(self, strat, window, input_backend, capture, comparator, clock, config,
                 launcher=None) -> None:
        self.strat = strat
        self.window = window
        self.input = input_backend
        self.capture = capture
        self.comparator = comparator
        self.clock = clock
        self.config = config
        if launcher is None:
            from .launcher import make_launcher
            launcher = make_launcher(config)
        self.launcher = launcher
        self.attempts: dict[str, int] = {}

    def _coerce(self, reason) -> FailureMode:
        if isinstance(reason, FailureMode):
            return reason
        try:
            return FailureMode(str(reason))
        except ValueError:
            return FailureMode.STUCK_SYNC

    def _over_budget(self, fm: FailureMode) -> bool:
        self.attempts[fm.value] = self.attempts.get(fm.value, 0) + 1
        n = self.attempts[fm.value]
        if n > self.config.max_attempts_per_cause:
            log.error("recovery cause %s exceeded cap (%d) -> STOP + notify", fm.value, n)
            return True
        log.info("recovery: handling %s (attempt %d/%d)", fm.value, n, self.config.max_attempts_per_cause)
        return False

    def classify(self, window: Optional[Frame], *, live: bool = True) -> FailureMode:
        if window is None:
            return FailureMode.STUCK_SYNC
        rec = self.strat.recovery
        if rec.disconnect and self._match(window, rec.disconnect):
            return FailureMode.DISCONNECTED
        if rec.wrong_map and self._match(window, rec.wrong_map):
            return FailureMode.WRONG_MAP
        re = self.strat.run_end
        if re and re.defeat and self._match(window, re.defeat):
            return FailureMode.DEFEAT
        if re and re.victory and self._match(window, re.victory):
            return FailureMode.VICTORY
        # FOCUS_LOST is a LIVE-only decision; never derive it from a captured/stale frame
        # (e.g. the stuck-sync reclassify), or a stuck sync gets mis-handled (recheck #w-classify-live)
        if live and not self.window.is_frontmost():
            return FailureMode.FOCUS_LOST
        return FailureMode.NONE

    def _match(self, window: Frame, det) -> bool:
        ref = self._load_ref(det.ref_frame, det)
        score = self.comparator.score(window, ref, self.config.sync_match_method, det.mask or None)
        return score >= det.threshold

    def _load_ref(self, ref_path: str, det) -> Frame:
        from .visual import load_reference

        try:
            f = load_reference(self.strat.resolve_frame(ref_path))
        except Exception as e:
            # A missing/corrupt recovery frame must NOT become a false match. The
            # comparator no longer treats a flat placeholder as matching a real
            # frame (D10/D11), so this flat fallback scores ~0 against live pixels;
            # warn loudly because load() should have validated these exist.
            log.warning("recovery reference %r unreadable (%s); detector will not fire", ref_path, e)
            f = Frame.labelled(ref_path)
        # label lets a MockComparator (tests) drive matching deterministically.
        f.label = getattr(det, "_label", None) or ref_path
        return f

    def handle(self, reason, *, scene: Optional[Frame] = None) -> Outcome:
        fm = self._coerce(reason)
        if fm in (FailureMode.NONE,):
            return Outcome.RESUME
        # DEFEAT/VICTORY are normal end-of-run terminators (classify() can return them,
        # incl. via a stuck-sync reclassify); they must NOT charge a recovery budget,
        # else >max_attempts healthy runs would STOP the bot (recheck #w3/#w4).
        if fm in (FailureMode.DEFEAT, FailureMode.VICTORY):
            return Outcome.REJOIN
        if self._over_budget(fm):
            return Outcome.STOP

        if fm == FailureMode.FOCUS_LOST:
            self.window.activate()
            if self.window.is_frontmost():
                self.attempts[fm.value] = 0  # confirmed -> fresh budget next time
                return Outcome.RESUME
            return Outcome.STOP
        if fm == FailureMode.DISCONNECTED:
            if self._reconnect():
                self.attempts[fm.value] = 0
            return Outcome.REJOIN  # unconfirmed -> counter climbs toward STOP
        if fm == FailureMode.KICKED:
            return Outcome.REJOIN  # already at lobby; loop re-runs join_sequence
        if fm in (FailureMode.WRONG_MAP, FailureMode.STUCK_SYNC, FailureMode.OUT_OF_CASH,
                  FailureMode.STATE_MISMATCH):
            if fm == FailureMode.STUCK_SYNC and scene is not None:
                deeper = self.classify(scene, live=False)  # reclassify the frame, not live focus
                if deeper not in (FailureMode.NONE, FailureMode.STUCK_SYNC):
                    # a stuck sync that's really a known cause -> don't burn the stuck_sync
                    # budget for the misclassification, or confirmed recoveries still hit STOP (recheck #7)
                    self.attempts[fm.value] = 0
                    return self.handle(deeper, scene=scene)
            if self._leave_and_reset():
                self.attempts[fm.value] = 0  # confirmed reach of lobby -> reset budget
            return Outcome.REJOIN
        # DEFEAT / VICTORY are normal loop terminators; just restart.
        return Outcome.REJOIN

    # --- visual confirmation (plan R17/§8.5: confirm, don't assume) ---
    def _confirm_at_lobby(self) -> bool:
        """True if we can see the TDS hub. None configured -> unconfirmable (False)."""
        det = self.strat.recovery.lobby_anchor
        if det is None:
            return False
        geo = self.window.get_geometry()
        return self._match(self.capture.grab_window(geo), det)

    def _relaunch_experience(self) -> bool:
        """Hard-disconnect-to-website fallback: re-open the server. Prefers the private-
        server link (so we always land back in the SAME server), then relaunch_url."""
        url = (getattr(self.config, "private_server_url", "")
               or getattr(self.strat.header, "private_server_url", "")
               or getattr(self.config, "relaunch_url", ""))
        if not url or self.config.dry_run:
            return False
        log.info("recovery: relaunching Roblox experience")
        return self.launcher.open_url(url)

    # --- Roblox-client-level actions (the stable backbone, plan section 8) ---
    def _reconnect(self) -> bool:
        """Reconnect (Enter is usually default-focused), then VISUALLY confirm (R17).

        Returns True only on a confirmed return to the hub, so an unconfirmable /
        failed reconnect keeps the per-cause counter climbing toward STOP (M5).
        """
        if self.config.dry_run:
            return False
        log.info("recovery: reconnect (best-effort)")
        try:
            self.input.press_key("enter")
            self.input.release_key("enter")
        except Exception:
            pass
        if self.strat.recovery.lobby_anchor is None:
            return False  # cannot confirm -> don't reset the budget
        if self._confirm_at_lobby():
            return True
        # dumped to the Roblox app/website instead of the hub -> relaunch fallback (§8.5)
        if self._relaunch_experience():
            return self._confirm_at_lobby()
        log.warning("recovery: could not confirm reconnect reached the hub")
        return False

    def _leave_and_reset(self) -> bool:
        """Esc -> recorded leave/reset sequence -> VISUALLY confirm we hit the hub (M17/§8).

        Returns True only on confirmed arrival at the lobby (else the per-cause
        cap bounds blind retries, M5).
        """
        if self.config.dry_run:
            return False
        log.info("recovery: leave + reset character via Roblox menu")
        try:
            self.input.press_key("esc")
            self.input.release_key("esc")
        except Exception:
            pass
        if self.strat.leave_reset_sequence:
            self._run_sequence(self.strat.leave_reset_sequence)
        else:
            log.warning("no leave_reset_sequence recorded; pressed Esc only (record one for a "
                        "reliable Leave/Reset on your Roblox client version)")
        if self.strat.recovery.lobby_anchor is None:
            return False  # unconfirmable; per-cause cap bounds the retries
        confirmed = self._confirm_at_lobby()
        if not confirmed:
            log.warning("recovery: could not confirm we reached the lobby after leave/reset")
        return confirmed

    def _run_sequence(self, events) -> None:
        from .geometry import Coordinates
        from .strat import (ClickEvent, DragEvent, KeyPressEvent, KeyReleaseEvent,
                            MouseMoveEvent, ScrollEvent, WaitEvent, expand_all)

        geo = self.window.get_geometry()
        coords = Coordinates(geo)
        # Honor the recorded inter-event timing (the recorder stores gaps as each
        # event's absolute t_ms, not as WaitEvents) so menu navigation isn't fired
        # back-to-back too fast for Roblox to register (R-recheck #5).
        t0 = self.clock.now_ms()
        for e in expand_all(events):
            self.clock.sleep_until(t0 + e.t_ms)
            if isinstance(e, WaitEvent):
                continue  # the sleep_until above already realized the delay
            elif isinstance(e, MouseMoveEvent):
                px, py = coords.norm_to_logical(e.pos)
                self.input.move(px, py, e.duration_ms)
            elif isinstance(e, ClickEvent):
                if e.pos is not None:
                    px, py = coords.norm_to_logical(e.pos)
                else:
                    px = py = None
                self.input.click(e.button, px, py, e.clicks, e.hold_ms or self.config.default_click_hold_ms)
            elif isinstance(e, DragEvent):
                fx, fy = coords.norm_to_logical(e.frm)
                tx, ty = coords.norm_to_logical(e.to)
                self.input.drag(e.button, fx, fy, tx, ty, e.duration_ms)
            elif isinstance(e, KeyPressEvent):
                self.input.press_key(e.key, e.modifiers)
            elif isinstance(e, KeyReleaseEvent):
                self.input.release_key(e.key, e.modifiers)
            elif isinstance(e, ScrollEvent):
                if e.pos is not None:
                    px, py = coords.norm_to_logical(e.pos)
                    self.input.move(px, py)
                self.input.scroll(e.dx, e.dy)
