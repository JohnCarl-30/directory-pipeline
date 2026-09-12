"""Async token-bucket limiter, keyed per host.

Rate limits are a property of the *upstream*, not of our process, so the bucket
is keyed by host and shared across every coroutine in the worker. Burst is
separate from steady-state rate: it lets a batch start fast without exceeding
the long-run average the upstream actually cares about.

`retry_after` lets a 429 handler push the whole host into a cooldown, so one
coroutine seeing a 429 slows all of its siblings instead of each discovering the
limit independently.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate: float  # tokens per second (steady state)
    burst: float  # bucket capacity
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _cooldown_until: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def __post_init__(self) -> None:
        self._tokens = self.burst
        self._updated = time.monotonic()

    async def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds spent waiting."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    delay = self._cooldown_until - now
                else:
                    elapsed = now - self._updated
                    self._updated = now
                    self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return waited
                    deficit = tokens - self._tokens
                    delay = deficit / self.rate if self.rate > 0 else 0.05
            # Sleep outside the lock so siblings can still observe cooldowns.
            await asyncio.sleep(delay)
            waited += delay

    async def penalize(self, seconds: float) -> None:
        """Apply a host-wide cooldown, e.g. after a 429 with Retry-After."""
        async with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)
            self._tokens = 0.0


class HostRateLimiter:
    """Lazily creates one bucket per host."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._guard = asyncio.Lock()

    async def bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            async with self._guard:
                if host not in self._buckets:
                    self._buckets[host] = TokenBucket(self.rate, float(self.burst))
        return self._buckets[host]

    async def acquire(self, host: str) -> float:
        return await (await self.bucket(host)).acquire()

    async def penalize(self, host: str, seconds: float) -> None:
        await (await self.bucket(host)).penalize(seconds)
