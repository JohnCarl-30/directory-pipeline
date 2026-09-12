"""Rate limiter and circuit breaker: the parts that keep a crawl polite."""

from __future__ import annotations

import asyncio
import time

import pytest

from directory_pipeline.scraping.circuit import Breaker, CircuitOpenError, State
from directory_pipeline.scraping.rate_limit import HostRateLimiter, TokenBucket


async def test_bucket_allows_burst_without_waiting():
    bucket = TokenBucket(rate=10.0, burst=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.05


async def test_bucket_throttles_beyond_burst():
    bucket = TokenBucket(rate=20.0, burst=2)
    await bucket.acquire()
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()  # must wait ~1/20s for a refill
    assert time.monotonic() - start >= 0.03


async def test_penalize_stalls_every_waiter_on_the_host():
    """A 429 seen by one coroutine must slow all of its siblings."""
    bucket = TokenBucket(rate=1000.0, burst=1000)
    await bucket.penalize(0.15)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    assert time.monotonic() - start >= 0.12


async def test_limiter_isolates_hosts():
    limiter = HostRateLimiter(rate=1000.0, burst=1000)
    await limiter.penalize("slow.test", 0.2)
    start = time.monotonic()
    await limiter.acquire("fast.test")  # different host, unaffected
    assert time.monotonic() - start < 0.05


async def test_breaker_opens_after_threshold_and_recovers():
    breaker = Breaker(failure_threshold=3, reset_timeout_s=0.1)

    for _ in range(3):
        await breaker.before_request("h")
        await breaker.on_failure()
    assert breaker.state is State.OPEN

    with pytest.raises(CircuitOpenError):
        await breaker.before_request("h")  # fails fast, no request sent

    await asyncio.sleep(0.12)
    await breaker.before_request("h")  # half-open trial allowed
    assert breaker.state is State.HALF_OPEN

    await breaker.on_success()
    assert breaker.state is State.CLOSED


async def test_half_open_failure_reopens_immediately():
    breaker = Breaker(failure_threshold=1, reset_timeout_s=0.05)
    await breaker.before_request("h")
    await breaker.on_failure()
    await asyncio.sleep(0.06)
    await breaker.before_request("h")
    await breaker.on_failure()
    assert breaker.state is State.OPEN
