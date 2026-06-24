"""Injectable time source.

Production uses :class:`RealClock`, whose sleeps are sliced and panic-aware
(plan M10) so a panic interrupts within ~15ms even mid-wait. Tests use
:class:`FakeClock`, which advances instantly and deterministically and can run a
hook on each sleep to simulate the world changing while the engine waits.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Protocol

from .errors import PanicAbort


class Clock(Protocol):
    def now_ms(self) -> float: ...
    def sleep(self, ms: float) -> None: ...
    def sleep_until(self, deadline_ms: float) -> None: ...


class RealClock:
    """Monotonic wall clock. ``should_abort`` is polled between sleep slices."""

    SLICE_MS = 15.0

    def __init__(self, should_abort: Optional[Callable[[], bool]] = None) -> None:
        self._t0 = time.monotonic()
        self._should_abort = should_abort or (lambda: False)

    def now_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def _check(self) -> None:
        if self._should_abort():
            raise PanicAbort("panic/stop requested during sleep")

    def sleep(self, ms: float) -> None:
        self.sleep_until(self.now_ms() + ms)

    def sleep_until(self, deadline_ms: float) -> None:
        self._check()
        while True:
            remaining = deadline_ms - self.now_ms()
            if remaining <= 0:
                return
            time.sleep(min(remaining, self.SLICE_MS) / 1000.0)
            self._check()


class FakeClock:
    """Deterministic test clock.

    ``on_sleep(new_now_ms)`` is invoked after each sleep advances the clock, so a
    test can mutate scripted comparator scores / capture frames to simulate the
    game reaching a state at a particular virtual time.
    """

    def __init__(self, on_sleep: Optional[Callable[[float], None]] = None) -> None:
        self._now = 0.0
        self.on_sleep = on_sleep
        self.should_abort: Callable[[], bool] = lambda: False
        self.slept_ms_total = 0.0

    def now_ms(self) -> float:
        return self._now

    def sleep(self, ms: float) -> None:
        if self.should_abort():
            raise PanicAbort("panic/stop requested during sleep")
        ms = max(0.0, ms)
        self._now += ms
        self.slept_ms_total += ms
        if self.on_sleep:
            self.on_sleep(self._now)

    def sleep_until(self, deadline_ms: float) -> None:
        self.sleep(max(0.0, deadline_ms - self._now))

    def advance(self, ms: float) -> None:
        self._now += ms
