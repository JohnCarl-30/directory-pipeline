"""robots.txt: group selection, failure policy, rate directives, single flight.

The interesting assertions here are the negative ones. A politeness gate that
merely raises before a request is easy to write and easy to get subtly wrong --
so several of these check `route.call_count == 0`, which is the only evidence
that the request was never sent rather than sent and discarded.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from directory_pipeline.config import Settings
from directory_pipeline.fixtures.mock_directory import ROBOTS_TXT
from directory_pipeline.scraping.client import (
    _UA_POOL,
    ResilientClient,
    RobotsDisallowedError,
)
from directory_pipeline.scraping.rate_limit import HostRateLimiter
from directory_pipeline.scraping.robots import (
    MAX_ROBOTS_BYTES,
    RobotsCache,
    origin_of,
    parse_robots,
    robots_url_for,
)

OURS = "directory-pipeline/0.1 (+https://example.com/bot; contact=devs@example.com)"


def polite_settings(**overrides) -> Settings:
    """Robots on, limits wide open so nothing in here waits on a bucket."""
    return Settings(crawl_rps=1000.0, crawl_burst=1000, request_timeout_s=2.0, **overrides)


def robots(body: str, *, status: int = 200) -> respx.Route:
    return respx.get("http://t.test/robots.txt").mock(
        return_value=httpx.Response(status, text=body)
    )


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def test_robots_is_obeyed_by_default():
    """The whole point. A crawler configured into politeness ships without it."""
    assert Settings().obey_robots is True
    assert Settings().rotate_user_agents is False


def test_the_configured_product_token_is_what_goes_on_the_wire():
    client = ResilientClient(polite_settings())
    assert client._user_agent() == Settings().user_agent
    assert "directory-pipeline" in client._user_agent()
    assert "+https://" in client._user_agent(), "a UA with no contact URL is not an identity"


def test_rotation_requires_opting_out_of_robots():
    """The two settings are individually reasonable and jointly incoherent."""
    with pytest.raises(ValueError, match="incompatible with OBEY_ROBOTS"):
        Settings(rotate_user_agents=True)

    rotating = Settings(rotate_user_agents=True, obey_robots=False)
    assert ResilientClient(rotating)._user_agent() in _UA_POOL


# --------------------------------------------------------------------------
# Failure policy: 4xx allows, 5xx forbids
# --------------------------------------------------------------------------


@respx.mock
async def test_a_missing_robots_txt_allows_everything():
    """404 means no rules were published, not that everything is forbidden."""
    robots("", status=404)
    page = respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings(), base_backoff_s=0.001) as client:
        response = await client.get("http://t.test/company/x")

    assert response.status == 200
    assert page.call_count == 1


@respx.mock
async def test_an_unavailable_robots_txt_forbids_everything():
    """5xx means the host cannot state its rules. Guessing favours the host."""
    robots("", status=503)
    page = respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings(), max_attempts=2, base_backoff_s=0.001) as client:
        with pytest.raises(RobotsDisallowedError) as exc:
            await client.get("http://t.test/company/x")

    assert "unavailable" in str(exc.value)
    assert page.call_count == 0, "the page was fetched despite an unreadable robots.txt"


@respx.mock
async def test_a_transport_failure_fetching_robots_forbids_everything():
    respx.get("http://t.test/robots.txt").mock(side_effect=httpx.ConnectError("no route"))
    page = respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200))

    async with ResilientClient(polite_settings(), max_attempts=2, base_backoff_s=0.001) as client:
        with pytest.raises(RobotsDisallowedError):
            await client.get("http://t.test/company/x")

    assert page.call_count == 0


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@respx.mock
async def test_a_disallowed_path_is_never_requested():
    robots("User-agent: *\nDisallow: /private/\n")
    page = respx.get("http://t.test/private/x").mock(return_value=httpx.Response(200, text="s"))

    async with ResilientClient(polite_settings()) as client:
        with pytest.raises(RobotsDisallowedError) as exc:
            await client.get("http://t.test/private/x")

    assert exc.value.url == "http://t.test/private/x"
    assert page.call_count == 0, "refused after sending is not refusing"


@respx.mock
async def test_a_disallow_is_not_retryable():
    """Permission does not change on retry, and Temporal must be told so.

    The activities forward `exc.retryable` into `ApplicationError`, so a True
    here would spend six attempts over ten minutes relearning a `Disallow`.
    """
    robots("User-agent: *\nDisallow: /\n")

    async with ResilientClient(polite_settings()) as client:
        with pytest.raises(RobotsDisallowedError) as exc:
            await client.get("http://t.test/anything")

    assert exc.value.retryable is False


@respx.mock
async def test_the_group_naming_our_product_token_wins_over_the_wildcard():
    """Site operators grant allowances by naming the bot. Read our own group.

    Rule order inside the group is deliberate: `urllib.robotparser` applies the
    *first* matching rule, not the longest, so `Disallow: /admin/` has to
    precede `Allow: /` to bite. See the note in scraping/robots.py.
    """
    robots(
        "User-agent: *\n"
        "Disallow: /\n"
        "\n"
        "User-agent: directory-pipeline\n"
        "Disallow: /admin/\n"
        "Allow: /\n"
    )
    page = respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        assert (await client.get("http://t.test/company/x")).status == 200
        with pytest.raises(RobotsDisallowedError):
            await client.get("http://t.test/admin/panel")

    assert page.call_count == 1


@respx.mock
async def test_rules_past_the_size_limit_are_not_parsed():
    """A multi-megabyte robots.txt is a misconfiguration or a tarpit."""
    filler = "# padding\n" * (MAX_ROBOTS_BYTES // 10 + 100)
    robots(f"User-agent: *\nAllow: /\n{filler}Disallow: /late/\n")
    page = respx.get("http://t.test/late/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        assert (await client.get("http://t.test/late/x")).status == 200

    assert page.call_count == 1


def test_an_empty_robots_txt_allows_everything():
    parser = parse_robots("")
    assert parser.can_fetch(OURS, "http://t.test/anything")


# --------------------------------------------------------------------------
# Rate directives
# --------------------------------------------------------------------------


@respx.mock
async def test_crawl_delay_lowers_the_host_rate():
    robots("User-agent: *\nCrawl-delay: 10\nAllow: /\n")
    respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        await client.get("http://t.test/company/x")
        bucket = await client.limiter.bucket("t.test")

    assert bucket.rate == pytest.approx(0.1), "Crawl-delay: 10 means one request per 10s"
    assert bucket.burst == 1.0, "a burst of 10 at 0.1 rps honors the average, not the request"


@respx.mock
async def test_the_stricter_of_crawl_delay_and_request_rate_wins():
    robots("User-agent: *\nCrawl-delay: 2\nRequest-rate: 1/10\nAllow: /\n")
    respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        await client.get("http://t.test/company/x")
        bucket = await client.limiter.bucket("t.test")

    assert bucket.rate == pytest.approx(0.1), "0.5 rps and 0.1 rps published; 0.1 applies"


async def test_a_host_directive_never_raises_our_configured_rate():
    """`Crawl-delay: 1` is not permission to crawl faster than CRAWL_RPS."""
    limiter = HostRateLimiter(0.5, 1)
    await limiter.constrain("t.test", 1.0)
    assert (await limiter.bucket("t.test")).rate == 0.5
    assert limiter.caps() == {}

    await limiter.constrain("t.test", 0.2)
    assert (await limiter.bucket("t.test")).rate == 0.2
    assert limiter.caps() == {"t.test": 0.2}


# --------------------------------------------------------------------------
# Fetching discipline
# --------------------------------------------------------------------------


@respx.mock
async def test_concurrent_first_requests_fetch_robots_once():
    """25 pages starting together must not open with 25 robots.txt requests."""
    route = robots("User-agent: *\nAllow: /\n")
    respx.get(url__regex=r"http://t\.test/company/\d+").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with ResilientClient(polite_settings()) as client:
        await asyncio.gather(*(client.get(f"http://t.test/company/{i}") for i in range(25)))

    assert route.call_count == 1


@respx.mock
async def test_robots_is_fetched_once_within_the_ttl():
    route = robots("User-agent: *\nAllow: /\n")
    respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        await client.get("http://t.test/company/x")
        await client.get("http://t.test/company/x")

    assert route.call_count == 1


@respx.mock
async def test_an_expired_entry_is_refetched():
    """A long-running worker must notice robots.txt changing under it."""
    route = robots("User-agent: *\nAllow: /\n")
    respx.get("http://t.test/company/x").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings(robots_cache_ttl_s=0.0)) as client:
        await client.get("http://t.test/company/x")
        await client.get("http://t.test/company/x")

    assert route.call_count == 2


@respx.mock
async def test_an_unavailable_robots_txt_expires_sooner_than_a_good_one():
    """A 5xx blocks the host, so it must not block it for the full hour."""

    async def fetch(url: str) -> tuple[int, str]:
        return (503, "")

    cache = RobotsCache(fetch, user_agent=OURS, ttl_s=3600.0, failure_ttl_s=60.0)
    await cache.check("http://t.test/p")
    entry = cache._entries["http://t.test"]

    assert entry.ttl_s == 60.0, "an outage should cost a minute of crawling, not an hour"


@respx.mock
async def test_each_origin_is_asked_separately():
    """robots.txt is per origin. One host's answer is not another's."""
    allowed = robots("User-agent: *\nAllow: /\n")
    denied = respx.get("http://u.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    respx.get("http://t.test/p").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings()) as client:
        assert (await client.get("http://t.test/p")).status == 200
        with pytest.raises(RobotsDisallowedError):
            await client.get("http://u.test/p")

    assert allowed.call_count == 1
    assert denied.call_count == 1


@respx.mock
async def test_fetching_robots_does_not_ask_robots_for_permission():
    """The recursion guard. Without it the first request never completes."""
    route = robots("User-agent: *\nDisallow: /\n")

    async with ResilientClient(polite_settings()) as client:
        with pytest.raises(RobotsDisallowedError):
            await client.get("http://t.test/p")

    # Disallow: / covers /robots.txt too, so a gate without the exemption
    # would either recurse or refuse to read the file it needs.
    assert route.call_count == 1


@respx.mock
async def test_turning_robots_off_skips_the_fetch_entirely():
    route = robots("User-agent: *\nDisallow: /\n")
    page = respx.get("http://t.test/p").mock(return_value=httpx.Response(200, text="ok"))

    async with ResilientClient(polite_settings(obey_robots=False)) as client:
        assert client.robots is None
        assert (await client.get("http://t.test/p")).status == 200

    assert route.call_count == 0
    assert page.call_count == 1


async def test_the_cache_snapshot_reports_what_each_origin_said():
    async def fetch(url: str) -> tuple[int, str]:
        return (404, "") if "t.test" in url else (503, "")

    cache = RobotsCache(fetch, user_agent=OURS)
    await cache.check("http://t.test/p")
    await cache.check("http://u.test/p")

    assert cache.snapshot() == {
        "http://t.test": "no robots.txt (404)",
        "http://u.test": "robots.txt unavailable (503)",
    }


# --------------------------------------------------------------------------
# URL handling, and the fixture's own rules
# --------------------------------------------------------------------------


def test_origin_keeps_scheme_and_port_and_drops_the_rest():
    assert origin_of("https://x.test:8443/a/b?q=1#f") == "https://x.test:8443"
    assert origin_of("http://x.test/a") == "http://x.test"
    assert robots_url_for("http://x.test/a/b") == "http://x.test/robots.txt"


def test_http_and_https_are_different_origins():
    assert origin_of("http://x.test/a") != origin_of("https://x.test/a")


def test_the_mock_directorys_private_path_is_disallowed_for_us():
    """The demo's refusal is a real rule, parsed by the real parser."""
    parser = parse_robots(ROBOTS_TXT)
    assert not parser.can_fetch(OURS, "http://localhost:8081/private/secret-listing")
    assert parser.can_fetch(OURS, "http://localhost:8081/company/harbor-point-labs")
    assert parser.can_fetch(OURS, "http://localhost:8081/directory/software")


def test_the_mock_directory_paces_generic_crawlers_but_not_ours():
    parser = parse_robots(ROBOTS_TXT)
    generic = parser.crawl_delay("some-other-bot")
    # typeshed types this `str | None` while CPython returns an int, so the
    # comparison goes through float() -- the same coercion RobotsCache uses.
    assert generic is not None and float(generic) == 2.0
    assert parser.crawl_delay(OURS) is None, "our group has no delay, so the demo runs fast"


# --------------------------------------------------------------------------
# Scope: crawling versus contracted APIs
# --------------------------------------------------------------------------


def test_the_enrichment_provider_opts_out_of_robots():
    """robots.txt governs crawling published content, not vendor API calls.

    Left on, a vendor whose API host has no reason to serve /robots.txt at all
    would halt enrichment the moment that 404 became a 502.
    """
    from directory_pipeline.enrichment.provider import EnrichmentProvider

    assert EnrichmentProvider(polite_settings()).client.robots is None


def test_the_crawler_does_not_opt_out():
    from directory_pipeline.scraping.crawler import DirectoryCrawler

    assert DirectoryCrawler(polite_settings()).client.robots is not None
