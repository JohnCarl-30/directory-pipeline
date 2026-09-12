"""Retry, backoff, proxy rotation, 429 handling -- against a mocked transport."""

from __future__ import annotations

import httpx
import pytest
import respx

from directory_pipeline.config import Settings
from directory_pipeline.scraping.client import (
    FetchError,
    ProxyPool,
    ResilientClient,
    _parse_retry_after,
)


def fast_settings(**overrides) -> Settings:
    return Settings(crawl_rps=1000.0, crawl_burst=1000, request_timeout_s=2.0, **overrides)


@respx.mock
async def test_retries_5xx_then_succeeds():
    route = respx.get("http://t.test/p").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, text="<html>ok</html>"),
        ]
    )
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        response = await client.get("http://t.test/p")
    assert response.status == 200
    assert response.attempts == 3
    assert route.call_count == 3


@respx.mock
async def test_does_not_retry_a_404():
    """A 404 is our fault, not theirs -- retrying just wastes the budget."""
    route = respx.get("http://t.test/missing").mock(return_value=httpx.Response(404))
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        with pytest.raises(FetchError) as exc:
            await client.get("http://t.test/missing")
    assert exc.value.status == 404
    assert exc.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_raises_after_exhausting_attempts():
    respx.get("http://t.test/dead").mock(return_value=httpx.Response(500))
    async with ResilientClient(fast_settings(), max_attempts=3, base_backoff_s=0.001) as client:
        with pytest.raises(FetchError) as exc:
            await client.get("http://t.test/dead")
    assert exc.value.retryable is True


@respx.mock
async def test_429_applies_a_host_wide_cooldown():
    respx.get("http://t.test/limited").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0.05"}),
            httpx.Response(200, text="ok"),
        ]
    )
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        await client.get("http://t.test/limited")
        bucket = await client.limiter.bucket("t.test")
        assert bucket._cooldown_until > 0  # every sibling coroutine now waits


@respx.mock
async def test_circuit_opens_and_fails_fast():
    respx.get("http://broken.test/x").mock(return_value=httpx.Response(500))
    async with ResilientClient(fast_settings(), max_attempts=2, base_backoff_s=0.001) as client:
        client.breakers._threshold = 2
        for _ in range(3):
            with pytest.raises(FetchError):
                await client.get("http://broken.test/x")
        assert client.breakers.snapshot()["broken.test"] == "open"


@respx.mock
async def test_transport_errors_are_retried():
    respx.get("http://t.test/flaky").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, text="ok")]
    )
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        assert (await client.get("http://t.test/flaky")).status == 200


def test_proxy_pool_round_robins_and_ejects():
    pool = ProxyPool(["http://p1", "http://p2"], eject_after=2)
    assert {pool.next(), pool.next()} == {"http://p1", "http://p2"}

    pool.report("http://p1", ok=False)
    pool.report("http://p1", ok=False)
    assert all(pool.next() == "http://p2" for _ in range(4))  # p1 parked

    pool.report("http://p1", ok=True)  # recovered
    assert {pool.next() for _ in range(6)} == {"http://p1", "http://p2"}


def test_empty_proxy_pool_goes_direct():
    pool = ProxyPool([])
    assert pool.enabled is False
    assert pool.next() is None


def test_all_proxies_parked_resets_rather_than_leaking_real_ip():
    pool = ProxyPool(["http://p1"], eject_after=1)
    pool.report("http://p1", ok=False)
    assert pool.next() == "http://p1"  # never None while proxies are configured


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), ("0.5", 0.5), (None, None), ("Wed, 21 Oct 2026 07:28:00 GMT", None)],
)
def test_parse_retry_after(header, expected):
    assert _parse_retry_after(header) == expected
