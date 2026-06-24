"""Command-line entrypoint: record | play | validate | calibrate | check-perms | smoke.

Functionality-first, no GUI. SIGINT/SIGTERM handlers are installed on the MAIN
thread and only set the panic Event; the engine runs on a worker thread so a
Ctrl-C is delivered promptly and releases all held inputs (plan M9).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone

from .config import Config, InputBackendKind, ScreenBackendKind, WindowBackendKind
from . import ui

CONSENT_PATH = os.path.expanduser("~/.tds_macro_consent")
log = logging.getLogger("tds_macro")


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def _apply_mock(config: Config) -> Config:
    config.input_backend = InputBackendKind.MOCK
    config.screen_backend = ScreenBackendKind.MOCK
    config.window_backend = WindowBackendKind.MOCK
    if config.window_rect_override is None:
        config.window_rect_override = (0, 0, 1600, 900)
    return config


def _build_config(args, overrides: dict | None = None) -> Config:
    config = Config()
    if overrides:
        config = config.with_overrides(overrides)
    for key in ("loop_count", "dry_run", "log_level", "frames_dir"):
        val = getattr(args, key, None)
        if val is not None:
            setattr(config, key, val)
    if getattr(args, "window_rect", None):
        config.window_rect_override = tuple(args.window_rect)
    if getattr(args, "mock", False) or sys.platform != "darwin":
        if sys.platform != "darwin" and not getattr(args, "mock", False):
            log.warning("not running on macOS; falling back to MOCK backends (no real input/capture)")
        _apply_mock(config)
    return config


def _build_backends(config: Config):
    from .window import make_window_provider
    from .capture import make_capture_backend
    from .input_backend import make_input_backend
    from .visual import make_comparator

    return (make_window_provider(config), make_input_backend(config),
            make_capture_backend(config), make_comparator())


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------- #
# signal-safe engine run (M9)
# --------------------------------------------------------------------------- #
def _run_with_signals(player, hotkeys):
    box: dict = {}

    def target():
        try:
            box["stats"] = player.run()
        except BaseException as e:  # noqa: BLE001 - surface, never silently swallow a worker crash
            box["error"] = e

    def handler(signum, frame):
        hotkeys.events.panic.set()
        hotkeys.events.stop.set()

    old_int = signal.signal(signal.SIGINT, handler)
    old_term = None
    try:
        old_term = signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass

    t = threading.Thread(target=target, daemon=True)
    status = ui.StatusLine()
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
            st = player.stats
            status.update(
                ui.style("● ", "cyan") + "state=" + ui.style(player.state.value, "bold")
                + f"  runs={st.runs}  " + ui.style(f"W{st.wins}", "green") + "/"
                + ui.style(f"L{st.losses}", "red")
                + f"  rec={st.recoveries}  sync_timeouts={st.sync_timeouts}"
            )
    except KeyboardInterrupt:
        hotkeys.events.panic.set()
        hotkeys.events.stop.set()
        t.join(5)
    finally:
        status.done()
        signal.signal(signal.SIGINT, old_int)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    if box.get("error") is not None and box.get("stats") is None:
        ui.err(f"run crashed: {type(box['error']).__name__}: {box['error']}")
    return box.get("stats")


def _check_consent(args) -> bool:
    if getattr(args, "accept_ban_risk", False):
        try:
            with open(CONSENT_PATH, "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        except Exception:
            pass
        return True
    return os.path.exists(CONSENT_PATH)


_BAN_WARNING = (
    "\n*** BAN-RISK ACKNOWLEDGEMENT REQUIRED ***\n"
    "Automating Roblox / Tower Defense Simulator violates the Roblox Terms of Use.\n"
    "Auto-farming with repetitive input is detectable by Roblox anti-cheat and CAN get\n"
    "your account suspended or permanently banned. This tool uses only screen capture +\n"
    "OS-level input (no memory injection), which is lower-risk than exploits but is STILL\n"
    "a ToU violation. Nothing here makes it safe.\n"
    "Re-run with --accept-ban-risk to acknowledge and proceed (saved to "
    f"{CONSENT_PATH}).\n"
)


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_validate(args) -> int:
    from .strat import load
    from .errors import StratValidationError

    try:
        st = load(args.strat, check_frames=not args.no_frames)
    except (StratValidationError, OSError) as e:
        ui.err(f"invalid strat: {args.strat}")
        print(e)
        return 1
    ui.ok(f"valid: {args.strat}")
    print("  " + "  ".join([
        ui.kv("map", st.header.map or "?"), ui.kv("difficulty", st.header.difficulty or "?"),
        ui.kv("events", len(st.events)), ui.kv("join", len(st.join_sequence)),
        ui.kv("leave_reset", len(st.leave_reset_sequence)),
    ]))
    print("  " + "  ".join([
        ui.kv("run_end", bool(st.run_end), good=bool(st.run_end)),
        ui.kv("expected_map_check", bool(st.expected_map_check), good=bool(st.expected_map_check)),
        ui.kv("wrong_map", bool(st.recovery.wrong_map), good=bool(st.recovery.wrong_map)),
        ui.kv("disconnect", bool(st.recovery.disconnect), good=bool(st.recovery.disconnect)),
        ui.kv("lobby_anchor", bool(st.recovery.lobby_anchor), good=bool(st.recovery.lobby_anchor)),
    ]))
    return 0


def cmd_check_perms(args) -> int:
    from . import permissions

    config = _build_config(args)
    window, _input, capture, _cmp = _build_backends(config)
    status = permissions.check_all(config, capture=capture, window=window)
    if status.ok:
        ui.ok("Permissions OK" + ("" if permissions.is_macos() else " (non-macOS: checks are no-ops)"))
        return 0
    ui.err("Permission problems:")
    for m in status.messages:
        print("  " + ui.style("- ", "red") + m)
    return 1


def cmd_record(args) -> int:
    from .strat import Header, save
    from .recorder import Recorder
    from .hotkeys import HotkeyManager
    from .clock import RealClock

    config = _build_config(args)
    config.frames_dir = args.frames_dir or "frames"
    window, input_backend, capture, _cmp = _build_backends(config)
    hk = HotkeyManager(config)
    hk.start()
    clock = RealClock(should_abort=hk.should_abort)
    rec = Recorder(window, input_backend, capture, config, hk, clock=clock)
    header = Header(name=args.name or "", map=args.map or "", difficulty=args.difficulty or "",
                    created=datetime.now(timezone.utc).isoformat(), created_by=os.environ.get("USER", ""))
    ui.info(f"Recording... play TDS now. Press {config.panic_hotkey} to stop, "
            f"{config.mark_sync_hotkey} to drop a sync point.")
    try:
        strat = rec.run(args.strat, header=header)
    except Exception as e:  # e.g. Roblox window not found at record start
        ui.err(f"recording failed: {e}")
        return 1
    finally:
        hk.stop()
    save(strat, args.strat)
    ui.ok(f"saved {len(strat.events)} events to {args.strat}")
    return 0


def cmd_play(args) -> int:
    from .strat import load
    from .errors import StratValidationError, PermissionsError
    from .engine import Player
    from .recovery import RecoveryController
    from .hotkeys import HotkeyManager
    from .clock import RealClock
    from . import permissions

    if not _check_consent(args):
        print(ui.style(_BAN_WARNING, "yellow"))
        return 2
    try:
        st = load(args.strat, check_frames=not args.no_frames)
    except (StratValidationError, OSError) as e:
        ui.err(f"could not load strat: {args.strat}")
        print(e)
        return 1

    try:
        config = _build_config(args, overrides=st.config_overrides)
    except (ValueError, TypeError) as e:
        ui.err(f"bad config_overrides in {args.strat}: {e}")
        return 1
    cfg_problems = config.validate()  # wire the (previously dead) range checks (R6)
    if cfg_problems:
        ui.err("invalid config:")
        for p in cfg_problems:
            print("  - " + p)
        return 1
    window, input_backend, capture, comparator = _build_backends(config)

    if permissions.is_macos() and not config.dry_run:
        try:
            permissions.require_permissions_or_exit(config, capture=capture, window=window)
        except PermissionsError as e:
            print(e)
            return 3

    hk = HotkeyManager(config)
    clock = RealClock(should_abort=hk.should_abort)
    stats = None
    try:
        hk.start()  # inside the try so its finally always stops the listener (R6)
        recovery = RecoveryController(st, window, input_backend, capture, comparator, clock, config)
        player = Player(st, window, input_backend, capture, comparator, clock, recovery, config, hotkeys=hk)
        print(ui.banner(f"play {os.path.basename(args.strat)}"))
        print("  " + "  ".join([ui.kv("loop_count", config.loop_count), ui.kv("dry_run", config.dry_run),
                                ui.kv("panic_hotkey", config.panic_hotkey)]))
        stats = _run_with_signals(player, hk)
    except Exception as e:  # construction (e.g. window not found) — fail gracefully
        ui.err(f"play could not start: {e}")
        return 1
    finally:
        hk.stop()
    if stats is None:
        return 1  # worker crashed (already reported); non-zero exit for CI/shell (D-r3)
    ui.ok("done  " + "  ".join([
        ui.kv("runs", stats.runs), ui.kv("restarts", stats.restarts),
        ui.kv("wins", stats.wins, good=True), ui.kv("losses", stats.losses, good=False),
        ui.kv("recoveries", stats.recoveries), ui.kv("sync_timeouts", stats.sync_timeouts),
        ui.kv("reason", stats.stopped_reason or "ok"),
    ]))
    return 0


def cmd_calibrate(args) -> int:
    """Dry-run visual gates: score each sync/detector against the live screen (R28)."""
    from .strat import load, SyncPointEvent
    from .errors import StratValidationError

    config = _build_config(args)
    try:
        st = load(args.strat, check_frames=not args.no_frames)
    except (StratValidationError, OSError) as e:
        ui.err(f"could not load strat: {args.strat}")
        print(e)
        return 1
    window, _input, capture, comparator = _build_backends(config)
    from .visual import load_reference

    print(ui.banner(f"calibrate {os.path.basename(args.strat)}"))
    geo = window.get_geometry()
    aspect_ok = abs(geo.aspect - st.header.window_aspect) <= config.aspect_warn_tolerance if st.header.window_aspect else None
    print("  " + "  ".join([
        ui.kv("window", f"{geo.w}x{geo.h}"), ui.kv("retina", geo.retina),
        ui.kv("aspect", f"{geo.aspect:.3f}", good=aspect_ok),
        ui.kv("recorded_aspect", f"{st.header.window_aspect:.3f}"),
    ]))
    any_sync = False
    for e in st.events:
        if isinstance(e, SyncPointEvent):
            any_sync = True
            live = capture.grab_region(geo, e.region)
            ref = load_reference(st.resolve_frame(e.ref_frame))
            ref.label = e.label
            score = comparator.score(live, ref, e.match or config.sync_match_method, e.mask or None)
            thr = e.threshold if e.threshold is not None else config.sync_default_threshold
            if score >= thr:
                ui.ok(f"sync {e.label!r}: " + ui.kv("score", f"{score:.3f}", good=True) + "  " + ui.kv("threshold", f"{thr:.3f}"))
            else:
                ui.err(f"sync {e.label!r}: " + ui.kv("score", f"{score:.3f}", good=False) + "  " + ui.kv("threshold", f"{thr:.3f}"))
    if not any_sync:
        ui.info("no sync_point events to calibrate")
    return 0


def cmd_smoke(args) -> int:
    """On-Mac first-run check: window, non-black capture, dry-run a click (S nice-to-have)."""
    from . import permissions

    config = _build_config(args)
    window, input_backend, capture, _cmp = _build_backends(config)
    print(ui.banner("smoke test"))
    perms = permissions.check_all(config, capture=capture, window=window)
    (ui.ok if perms.ok else ui.err)("permissions: " + ("OK" if perms.ok else "MISSING"))
    try:
        geo = window.get_geometry()
        ui.ok(f"window found: {geo.w}x{geo.h} @ ({geo.x},{geo.y}) retina {geo.retina}")
    except Exception as e:
        ui.err(f"window NOT found: {e}")
        return 1
    try:
        frame = capture.grab_window(geo)
        ui.ok(f"captured frame: {frame.size}")
    except Exception as e:
        ui.err(f"capture failed: {e}")
    (ui.ok if window.is_frontmost() else ui.warn)(f"frontmost: {window.is_frontmost()}")
    ui.info("smoke complete")
    return 0


# --------------------------------------------------------------------------- #
def _add_common(p):
    p.add_argument("--mock", action="store_true", help="force mock backends (no real input/capture)")
    p.add_argument("--window-rect", type=int, nargs=4, metavar=("X", "Y", "W", "H"),
                   help="override window rect (for mock/testing)")
    p.add_argument("--log-level", default=None, help="DEBUG|INFO|WARN|ERROR")
    p.add_argument("--frames-dir", default=None)
    p.add_argument("--no-frames", action="store_true", help="skip reference-frame existence checks")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tds_macro", description="TDS macro: record/play with visual-sync + recovery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="record your play into a strat file")
    pr.add_argument("strat"); pr.add_argument("--name"); pr.add_argument("--map"); pr.add_argument("--difficulty")
    _add_common(pr); pr.set_defaults(func=cmd_record)

    pp = sub.add_parser("play", help="replay a strat with visual-sync + auto-loop")
    pp.add_argument("strat")
    pp.add_argument("--loop-count", type=int, default=None)
    pp.add_argument("--dry-run", action="store_true", default=None)
    pp.add_argument("--accept-ban-risk", action="store_true")
    _add_common(pp); pp.set_defaults(func=cmd_play)

    pv = sub.add_parser("validate", help="validate a strat file")
    pv.add_argument("strat"); _add_common(pv); pv.set_defaults(func=cmd_validate)

    pc = sub.add_parser("calibrate", help="score each sync against the live screen")
    pc.add_argument("strat"); _add_common(pc); pc.set_defaults(func=cmd_calibrate)

    pk = sub.add_parser("check-perms", help="check macOS Accessibility / Screen Recording")
    _add_common(pk); pk.set_defaults(func=cmd_check_perms)

    ps = sub.add_parser("smoke", help="quick on-Mac sanity check")
    _add_common(ps); ps.set_defaults(func=cmd_smoke)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "log_level", None) or "INFO")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
