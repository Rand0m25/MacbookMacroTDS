"""macOS permission self-checks (plan R01/R02/S7).

pynput silently no-ops without Accessibility, and mss returns black frames
without Screen Recording — both fail with no exception. We detect both up front
and tell the user exactly which host app to authorize. On non-macOS (the Linux
dev box / mock backends) these are no-ops returning True.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass
class PermissionStatus:
    accessibility: bool = True
    screen_recording: bool = True
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.accessibility and self.screen_recording


def is_macos() -> bool:
    return sys.platform == "darwin"


def check_accessibility(prompt: bool = False) -> bool:
    if not is_macos():
        return True
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        if prompt:
            return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
        return bool(AXIsProcessTrusted())
    except Exception:
        # If we cannot even check, assume missing so we surface guidance.
        return False


def check_screen_recording(capture=None, geo=None) -> bool:
    """Authoritatively ask macOS (CGPreflightScreenCaptureAccess), falling back to a variance
    heuristic. The heuristic alone can't win: a DENIED capture and a legit all-black loading screen
    are pixel-identical, so it oscillates between false-grant and false-deny (round 23 #13)."""
    if not is_macos():
        return True
    try:
        import Quartz  # type: ignore
        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is not None:
            return bool(preflight())  # definitive on macOS 10.15+
    except Exception:
        pass  # old macOS / no Quartz -> fall back to the heuristic below
    if capture is None or geo is None:
        return True
    try:

        frame = capture.grab_window(geo)
        arr = frame.as_numpy().astype("float64")
        # Use COLOR channels only: a real macOS denied capture is RGB(0,0,0) but OPAQUE (alpha=255),
        # so averaging all 4 BGRA channels gives mean ~63.75 and the denial heuristic never fires
        # (false "granted"). Strip alpha to test the actual pixels (round 23 #9).
        if arr.ndim == 3 and arr.shape[2] >= 4:
            arr = arr[:, :, :3]
        # An off-screen window gets clamped to a tiny (even 1x1) in-bounds sliver by the capture
        # backend; a tiny/uniform grab has var()==0 and can be dark, which would FALSELY read as
        # denied. Too-small to judge -> inconclusive (assume granted), not denied (round 22 #O).
        if arr.shape[0] * arr.shape[1] < 64:
            return True
        # macOS denial returns a perfectly flat BLACK frame. A legitimately near-uniform
        # window (dark scene, loading screen, solid-colour UI panel) still has a nonzero
        # mean, so only treat flat-AND-black as denied (avoids false negatives).
        denied = float(arr.var()) < 1e-6 and float(arr.mean()) < 1.0
        return not denied
    except Exception:
        return False


def host_app_hint() -> str:
    return (
        f"the app that launched Python (interpreter: {sys.executable}). "
        "Usually that is Terminal.app, iTerm, or VS Code — grant the permission to "
        "THAT app, then FULLY QUIT and relaunch it."
    )


def check_all(config, capture=None, window=None) -> PermissionStatus:
    status = PermissionStatus()
    if not is_macos():
        return status

    geo = None
    if window is not None:
        try:
            geo = window.get_geometry()
        except Exception as e:  # window not found is its own error elsewhere
            status.messages.append(f"Could not locate the Roblox window: {e}")

    status.accessibility = check_accessibility(prompt=True)
    if not status.accessibility:
        status.messages.append(
            "Accessibility permission is MISSING (mouse/keyboard control will silently "
            "do nothing). Grant it in System Settings > Privacy & Security > Accessibility to "
            + host_app_hint()
        )

    if geo is None:
        # We couldn't even locate the window, so the capture check can't run —
        # do NOT report Screen Recording as granted on an un-runnable check.
        status.screen_recording = False
        status.messages.append(
            "Could not verify Screen Recording permission because the Roblox window was not "
            "found (is Roblox running?)."
        )
    else:
        status.screen_recording = check_screen_recording(capture, geo)
        if not status.screen_recording:
            status.messages.append(
                "Screen Recording permission is MISSING (captures come back black, so visual-sync "
                "sees nothing). Grant it in System Settings > Privacy & Security > Screen Recording to "
                + host_app_hint()
            )
    return status


def require_permissions_or_exit(config, capture=None, window=None) -> PermissionStatus:
    from .errors import PermissionsError

    status = check_all(config, capture=capture, window=window)
    if not status.ok:
        raise PermissionsError("\n".join(status.messages))
    return status
