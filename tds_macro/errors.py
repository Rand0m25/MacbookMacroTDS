"""Shared exception types for the TDS macro.

Kept dependency-free so the whole package can import this on any platform.
"""

from __future__ import annotations


class TdsMacroError(Exception):
    """Base class for all errors raised by this package."""


class PanicAbort(TdsMacroError):
    """Raised from deep inside sleeps / interpolation / capture loops when the

    global panic (or stop) Event is set, so the engine's ``finally`` can unwind
    and release every held input (see plan M9/M10).
    """


class StratValidationError(TdsMacroError):
    """A strat file failed validation.

    Carries the full list of human-readable problems so the CLI can show every
    error at once instead of one-at-a-time (plan M12 / R24).
    """

    def __init__(self, problems: list[str], path: str | None = None) -> None:
        self.problems = list(problems)
        self.path = path
        header = f"Invalid strat file{f' {path!r}' if path else ''}:"
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{header}\n{body}")


class PermissionsError(TdsMacroError):
    """A required macOS permission (Accessibility / Screen Recording) is missing."""


class WindowNotFoundError(TdsMacroError):
    """The Roblox window could not be located."""


class RecoveryExhausted(TdsMacroError):
    """A recovery cause exceeded its per-cause attempt budget (plan M5)."""
