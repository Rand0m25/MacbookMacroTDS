"""tds_macro — a resolution-independent, visually self-correcting macro for
Roblox Tower Defense Simulator.

Public API only. Platform backends (pynput/mss/Quartz/numpy/opencv) are NEVER
imported here — they live behind factory functions so ``import tds_macro``
succeeds on any platform, including the numpy-less smoke test (plan M11).
"""

from __future__ import annotations

__version__ = "0.1.0"

# value types (zero OS deps)
from .config import (
    Config, MatchMethod, InputBackendKind, ScreenBackendKind, WindowBackendKind,
)
from .geometry import Point, Rect, WindowGeometry, Coordinates
from .frame import Frame
from .clock import Clock, RealClock, FakeClock

# strat model + io
from .strat import StratFile, Header, load, save, parse, expand_macro, expand_all

# application
from .recorder import Recorder, EventCoalescer
from .engine import Player, RunStats, RunState
from .recovery import RecoveryController, FailureMode, Outcome
from .hotkeys import HotkeyManager, HotkeyEvents

# Protocols (for typing / DI)
from .window import WindowProvider
from .capture import CaptureBackend
from .input_backend import InputBackend
from .visual import Comparator, SceneClass

# factory functions (resolve concrete backends lazily)
from .window import make_window_provider
from .capture import make_capture_backend
from .input_backend import make_input_backend
from .visual import make_comparator

__all__ = [
    "__version__",
    "Config", "MatchMethod", "InputBackendKind", "ScreenBackendKind", "WindowBackendKind",
    "Point", "Rect", "WindowGeometry", "Coordinates", "Frame",
    "Clock", "RealClock", "FakeClock",
    "StratFile", "Header", "load", "save", "parse", "expand_macro", "expand_all",
    "Recorder", "EventCoalescer",
    "Player", "RunStats", "RunState",
    "RecoveryController", "FailureMode", "Outcome",
    "HotkeyManager", "HotkeyEvents",
    "WindowProvider", "CaptureBackend", "InputBackend", "Comparator", "SceneClass",
    "make_window_provider", "make_capture_backend", "make_input_backend", "make_comparator",
]
