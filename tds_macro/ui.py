"""Tiny stdlib ANSI console UI — no third-party deps.

Honours NO_COLOR and non-TTY output (auto-plain). Used to make the CLI output
(validate/calibrate/play status) clean and readable without a GUI framework.
"""

from __future__ import annotations

import os
import sys

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"

_CODES = {
    "reset": "0", "bold": "1", "dim": "2",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90",
}


def style(text: str, *names: str) -> str:
    if not _ENABLED or not names:
        return text
    seq = "".join(f"\033[{_CODES[n]}m" for n in names if n in _CODES)
    return f"{seq}{text}\033[0m"


def supports_color() -> bool:
    return _ENABLED


def banner(title: str) -> str:
    line = "─" * (len(title) + 2)
    return style(f"┌{line}┐\n│ {title} │\n└{line}┘", "cyan", "bold")


def ok(msg: str) -> None:
    print(f"{style('✓', 'green', 'bold')} {msg}")


def warn(msg: str) -> None:
    print(f"{style('!', 'yellow', 'bold')} {style(msg, 'yellow')}")


def err(msg: str) -> None:
    print(f"{style('✗', 'red', 'bold')} {style(msg, 'red')}")


def info(msg: str) -> None:
    print(f"{style('•', 'blue')} {msg}")


def kv(key: str, value, good: bool | None = None) -> str:
    v = str(value)
    if good is True:
        v = style(v, "green")
    elif good is False:
        v = style(v, "red")
    return f"{style(key, 'grey')}={style(v, 'bold')}"


class StatusLine:
    """A single rewriting status line (carriage-return), TTY-only."""

    def __init__(self) -> None:
        self._last = 0
        self._on = _ENABLED

    def update(self, text: str) -> None:
        if not self._on:
            return
        pad = max(0, self._last - len(text))
        sys.stdout.write("\r" + text + " " * pad)
        sys.stdout.flush()
        self._last = len(text)

    def done(self, text: str = "") -> None:
        if not self._on:
            if text:
                print(text)
            return
        self.update(text)
        sys.stdout.write("\n")
        sys.stdout.flush()
