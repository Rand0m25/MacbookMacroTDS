"""Opens a URL at the OS level — used to join a Roblox private server by its link
(Feature A) and as the hard RELAUNCH_EXPERIENCE recovery fallback.

Mockable behind a Protocol: the real macOS impl shells out to ``open`` (list form,
never ``shell=True``); tests / Linux get a :class:`MockLauncher` that just records
the calls. The factory picks the mock whenever a mock backend is active.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger("tds_macro.launcher")


class Launcher(Protocol):
    def open_url(self, url: str) -> bool: ...


class MacLauncher:
    """macOS: `open <url>` hands the link to the registered handler (browser or the
    Roblox player protocol). No shell -> the URL can't inject shell commands."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def open_url(self, url: str) -> bool:
        if not url:
            return False
        if self.dry_run:
            log.info("[dry-run] would open %s", url)
            return False
        try:
            import subprocess

            subprocess.run(["open", url], check=False, capture_output=True, timeout=5)
            return True
        except Exception:
            log.warning("failed to open url %s", url)
            return False


class MockLauncher:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def open_url(self, url: str) -> bool:
        self.opened.append(url)
        return bool(url)


def make_launcher(config) -> Launcher:
    from .config import InputBackendKind, WindowBackendKind

    if (config.window_backend == WindowBackendKind.MOCK
            or config.input_backend == InputBackendKind.MOCK):
        return MockLauncher()
    return MacLauncher(dry_run=config.dry_run)
