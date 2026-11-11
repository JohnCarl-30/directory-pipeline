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
    """Retry mechanics, with the politeness gate out of the way.

    `obey_robots=False` keeps these tests about backoff and classification: with
    it on, every case below would also need a `/robots.txt` route, and a missing
    one would fail as a robots error rather than as the retry bug under test.
    The gate's own behaviour -- including that it is on by default -- is
    tests/test_robots.py.
    """
    return Settings(
        crawl_rps=1000.0,
        crawl_burst=1000,
        request_timeout_s=2.0,
        obey_robots=False,
        **overrides,
    )


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


# --- redirects ----------------------------------------------------------------


@respx.mock
async def test_redirects_are_followed_hop_by_hop():
    first = respx.get("http://t.test/old").mock(
        return_value=httpx.Response(301, headers={"Location": "/new"})
    )
    second = respx.get("http://t.test/new").mock(return_value=httpx.Response(200, text="moved"))
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        response = await client.get("http://t.test/old")
    assert response.status == 200
    assert response.text == "moved"
    assert response.url == "http://t.test/new"
    assert first.call_count == 1 and second.call_count == 1


@respx.mock
async def test_a_redirect_into_a_disallowed_path_is_never_fetched():
    """The hop is a new URL, so robots.txt is asked again -- not just for the first."""
    from directory_pipeline.scraping.client import RobotsDisallowedError

    respx.get("http://t.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    respx.get("http://t.test/company/x").mock(
        return_value=httpx.Response(302, headers={"Location": "/private/x"})
    )
    private = respx.get("http://t.test/private/x").mock(return_value=httpx.Response(200))
    settings = Settings(crawl_rps=1000.0, crawl_burst=1000, request_timeout_s=2.0)
    async with ResilientClient(settings, base_backoff_s=0.001) as client:
        with pytest.raises(RobotsDisallowedError):
            await client.get("http://t.test/company/x")
    assert private.call_count == 0


@respx.mock
async def test_a_cross_host_redirect_reads_the_other_hosts_robots_txt():
    respx.get("http://a.test/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("http://a.test/x").mock(
        return_value=httpx.Response(302, headers={"Location": "http://b.test/x"})
    )
    other_robots = respx.get("http://b.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    target = respx.get("http://b.test/x").mock(return_value=httpx.Response(200))
    settings = Settings(crawl_rps=1000.0, crawl_burst=1000, request_timeout_s=2.0)
    async with ResilientClient(settings, base_backoff_s=0.001) as client:
        with pytest.raises(FetchError):
            await client.get("http://a.test/x")
    assert other_robots.call_count == 1
    assert target.call_count == 0


@respx.mock
async def test_a_redirect_loop_is_terminal():
    respx.get("http://t.test/loop").mock(
        return_value=httpx.Response(302, headers={"Location": "/loop"})
    )
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        with pytest.raises(FetchError) as exc:
            await client.get("http://t.test/loop")
    assert exc.value.retryable is False


@respx.mock
async def test_303_turns_a_post_into_a_get_without_the_body():
    respx.post("http://t.test/form").mock(
        return_value=httpx.Response(303, headers={"Location": "/done"})
    )
    done = respx.get("http://t.test/done").mock(return_value=httpx.Response(200, text="ok"))
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        response = await client.request("POST", "http://t.test/form", json={"a": 1})
    assert response.status == 200
    assert done.calls.last.request.content == b""


# --- the breaker's half-open slot -----------------------------------------------


async def _half_open(client: ResilientClient, host: str):
    from directory_pipeline.scraping.circuit import State

    breaker = client.breakers.get(host)
    breaker.state = State.OPEN
    breaker.opened_at = 0.0  # long past the reset timeout
    return breaker


@respx.mock
async def test_a_cancelled_trial_request_hands_the_half_open_slot_back():
    """A trial that is cancelled must not leave the host refused forever."""
    import asyncio

    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.sleep(60)

    respx.get("http://t.test/slow").mock(side_effect=hang)
    respx.get("http://t.test/fast").mock(return_value=httpx.Response(200, text="ok"))
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        await _half_open(client, "t.test")
        task = asyncio.create_task(client.get("http://t.test/slow"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Before the fix this raised CircuitOpenError for the life of the worker.
        assert (await client.get("http://t.test/fast")).status == 200
        assert client.breakers.snapshot()["t.test"] == "closed"


@respx.mock
async def test_a_non_transport_httpx_error_is_a_terminal_fetch_error():
    respx.get("http://t.test/bad").mock(side_effect=httpx.DecodingError("garbled"))
    respx.get("http://t.test/fast").mock(return_value=httpx.Response(200, text="ok"))
    async with ResilientClient(fast_settings(), base_backoff_s=0.001) as client:
        await _half_open(client, "t.test")
        with pytest.raises(FetchError) as exc:
            await client.get("http://t.test/bad")
        assert exc.value.retryable is False
        assert (await client.get("http://t.test/fast")).status == 200
