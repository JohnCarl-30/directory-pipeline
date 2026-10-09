"""Async token-bucket limiter, keyed per host.

Rate limits are a property of the *upstream*, not of our process, so the bucket
is keyed by host and shared across every coroutine in the worker. Burst is
separate from steady-state rate: it lets a batch start fast without exceeding
the long-run average the upstream actually cares about.

`retry_after` lets a 429 handler push the whole host into a cooldown, so one
coroutine seeing a 429 slows all of its siblings instead of each discovering the
limit independently.

`constrain` is the other direction a limit can arrive from: a host that states
a `Crawl-delay` in robots.txt has told us its rate directly, rather than making
us infer it from 429s. That path only ever lowers the rate -- see the method.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ..observability import get_logger

log = get_logger(__name__)


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

    async def constrain(self, rate: float) -> bool:
        """Lower the steady-state rate to `rate`. Never raises it.

        One-directional on purpose. This is how a `Crawl-delay` from robots.txt
        reaches the bucket, and a host asking to be crawled slowly is not
        offering permission to crawl a different host quickly -- so the
        configured rate stays the ceiling and this only moves the floor down.

        Burst drops with it: a bucket holding 10 tokens at 0.1 rps would let a
        batch fire ten immediate requests at a host that asked for one every ten
        seconds, which honors the average and violates the request.
        """
        if rate <= 0 or rate >= self.rate:
            return False
        async with self._lock:
            self.rate = rate
            self.burst = min(self.burst, 1.0)
            self._tokens = min(self._tokens, self.burst)
            return True


class HostRateLimiter:
    """Lazily creates one bucket per host."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._caps: dict[str, float] = {}
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

    async def constrain(self, host: str, rate: float) -> None:
        """Cap one host's rate, e.g. from its `Crawl-delay`.

        The cap is remembered so the common path -- every request after the
        first -- costs a dict lookup instead of taking the bucket's lock to
        re-apply a cap that is already in force.
        """
        if self._caps.get(host) == rate:
            return
        if await (await self.bucket(host)).constrain(rate):
            self._caps[host] = rate
            log.info("ratelimit.constrained", host=host, rps=rate)

    def caps(self) -> dict[str, float]:
        return dict(self._caps)
