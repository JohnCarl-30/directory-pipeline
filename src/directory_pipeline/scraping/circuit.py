"""Per-host circuit breaker.

Without this, a directory that starts 500ing burns the entire retry budget of
every in-flight activity before Temporal ever sees a failure. The breaker fails
fast instead, so the workflow's own retry policy -- which can back off for
minutes -- takes over.

States: closed -> (failures exceed threshold) -> open -> (after reset timeout)
-> half_open -> (one trial succeeds) -> closed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    def __init__(self, host: str, retry_in: float) -> None:
        super().__init__(f"circuit open for {host}; retry in {retry_in:.1f}s")
        self.host = host
        self.retry_in = retry_in


@dataclass
class Breaker:
    failure_threshold: int = 5
    reset_timeout_s: float = 30.0
    half_open_max: int = 1

    state: State = State.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    _half_open_inflight: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def before_request(self, host: str) -> None:
        async with self._lock:
            if self.state is State.OPEN:
                elapsed = time.monotonic() - self.opened_at
                if elapsed < self.reset_timeout_s:
                    raise CircuitOpenError(host, self.reset_timeout_s - elapsed)
                self.state = State.HALF_OPEN
                self._half_open_inflight = 0
            if self.state is State.HALF_OPEN:
                if self._half_open_inflight >= self.half_open_max:
                    raise CircuitOpenError(host, self.reset_timeout_s)
                self._half_open_inflight += 1

    async def on_success(self) -> None:
        async with self._lock:
            self.failures = 0
            self._half_open_inflight = 0
            self.state = State.CLOSED

    async def on_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            self._half_open_inflight = 0
            if self.state is State.HALF_OPEN or self.failures >= self.failure_threshold:
                self.state = State.OPEN
                self.opened_at = time.monotonic()


class BreakerRegistry:
    def __init__(self, failure_threshold: int = 5, reset_timeout_s: float = 30.0) -> None:
        self._threshold = failure_threshold
        self._reset = reset_timeout_s
        self._breakers: dict[str, Breaker] = {}

    def get(self, host: str) -> Breaker:
        if host not in self._breakers:
            self._breakers[host] = Breaker(self._threshold, self._reset)
        return self._breakers[host]

    def snapshot(self) -> dict[str, str]:
        return {h: b.state.value for h, b in self._breakers.items()}
