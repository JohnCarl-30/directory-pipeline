"""robots.txt, fetched once per origin and actually obeyed.

This sits in the HTTP client rather than in the crawler on purpose. The crawler
is one caller; enrichment, a one-off script and anything added later are others,
and a politeness rule enforced in one caller is a rule the next caller forgets.
Putting the gate below every outbound request makes "we respect robots.txt" a
property of the process instead of a property of one code path.

Three decisions worth knowing, because the standard leaves them to the crawler:

  * **A missing robots.txt allows everything, a broken one allows nothing.**
    RFC 9309 draws the line at the status class: 4xx means "no rules exist, you
    are unrestricted", 5xx means "the server cannot tell you the rules right
    now", and guessing in the second case is how a crawler hammers a host that
    is already in trouble. Unavailability is cached for `failure_ttl_s` rather
    than `ttl_s`, so a blip costs a minute of crawling, not an hour.

  * **Group selection uses the product token, not a browser string.** The
    parser matches `User-agent:` groups against the token before the first
    slash of our UA, so the rules that apply are the ones written for
    `directory-pipeline`. Claiming to be Chrome while reading a group written
    for us would be reading rules we then do not identify as -- which is why
    `Settings` refuses that combination outright rather than letting it be a
    runtime surprise.

  * **`Crawl-delay` and `Request-rate` lower the token bucket, and never raise
    it.** The stricter of the host's directive and our own configured rate
    wins. A site asking for one request every ten seconds is not an invitation
    to go faster than `CRAWL_RPS` when it asks for one every two.

Concurrent first-touches of a host collapse into a single fetch. Without that,
a batch of 25 pages starting at once would open with 25 identical robots.txt
requests, which is a rude way to announce that you intend to be polite.

One honest limitation. Parsing is `urllib.robotparser`, which resolves rules by
*first* match within a group; RFC 9309 specifies the longest match. So a group
reading `Allow: /` then `Disallow: /admin/` is read here as allowing /admin/,
where a conforming parser would refuse it. The divergence only shows up in
groups that both allow and deny overlapping prefixes, and it errs toward
fetching -- which is the wrong direction to err, and the reason this is written
down rather than left to be discovered. Replacing the parser with one that
implements longest-match is the fix if this crawler ever points at a site whose
robots.txt is written that way.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from ..observability import METRICS, get_logger

log = get_logger(__name__)

# RFC 9309 asks crawlers to parse at least 500 KiB and permits ignoring the
# rest. A robots.txt larger than this is a misconfiguration or a tarpit.
MAX_ROBOTS_BYTES = 512 * 1024


class RobotsFetcher(Protocol):
    """Fetches a robots.txt and reports `(status, body)`.

    Status 0 means the request never produced one -- a timeout, a DNS failure,
    a tripped breaker. Returning it rather than raising keeps this module free
    of the client's exception types, and the client free of a second retry loop.
    """

    async def __call__(self, url: str) -> tuple[int, str]: ...


@dataclass(frozen=True)
class Verdict:
    """What robots.txt says about one URL."""

    allowed: bool
    reason: str
    crawl_delay: float | None = None
    max_rps: float | None = None


@dataclass
class _Entry:
    parser: RobotFileParser
    fetched_at: float
    ttl_s: float
    reason: str

    def expired(self, now: float) -> bool:
        return now - self.fetched_at >= self.ttl_s


def origin_of(url: str) -> str:
    """The scheme+authority a robots.txt governs.

    Keyed on the origin, not the host: robots.txt is served per origin, and
    `http://x` and `https://x` are entitled to different rules. Collapsing them
    would apply one host's answer to a port it never spoke for.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def robots_url_for(url: str) -> str:
    return f"{origin_of(url)}/robots.txt"


def parse_robots(body: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(body[:MAX_ROBOTS_BYTES].splitlines())
    return parser


# The two synthetic bodies stand in for "no rules" and "no permission". Running
# them through the same parser rather than setting its `allow_all` /
# `disallow_all` flags keeps this to the documented API -- those attributes are
# real but undeclared in typeshed, and the one-line shortcut would have cost a
# pair of `type: ignore`s on a behaviour the parser already expresses.
_NO_RULES = ""
_NO_PERMISSION = "User-agent: *\nDisallow: /\n"


def _permissive() -> RobotFileParser:
    return parse_robots(_NO_RULES)


def _restrictive() -> RobotFileParser:
    return parse_robots(_NO_PERMISSION)


class RobotsCache:
    """Per-origin robots.txt with a TTL, single-flighted."""

    def __init__(
        self,
        fetch: RobotsFetcher,
        *,
        user_agent: str,
        ttl_s: float = 3600.0,
        failure_ttl_s: float = 60.0,
    ) -> None:
        self._fetch = fetch
        self.user_agent = user_agent
        self.ttl_s = ttl_s
        self.failure_ttl_s = failure_ttl_s
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, origin: str) -> asyncio.Lock:
        if origin not in self._locks:
            async with self._guard:
                if origin not in self._locks:
                    self._locks[origin] = asyncio.Lock()
        return self._locks[origin]

    async def _entry(self, origin: str) -> _Entry:
        now = time.monotonic()
        cached = self._entries.get(origin)
        if cached is not None and not cached.expired(now):
            METRICS.incr("robots.cache_hit")
            return cached

        lock = await self._lock_for(origin)
        async with lock:
            # A sibling may have fetched it while we waited for the lock. This
            # recheck is the single-flight: 25 coroutines, one robots.txt.
            cached = self._entries.get(origin)
            if cached is not None and not cached.expired(time.monotonic()):
                METRICS.incr("robots.cache_hit")
                return cached

            entry = await self._load(origin)
            self._entries[origin] = entry
            return entry

    async def _load(self, origin: str) -> _Entry:
        url = f"{origin}/robots.txt"
        METRICS.incr("robots.fetch")
        status, body = await self._fetch(url)

        if 200 <= status < 300:
            log.info("robots.loaded", origin=origin, bytes=len(body))
            return _Entry(parse_robots(body), time.monotonic(), self.ttl_s, "robots.txt")

        if 400 <= status < 500:
            # No rules published. Unrestricted, per RFC 9309 s2.3.1.2.
            return _Entry(
                _permissive(),
                time.monotonic(),
                self.ttl_s,
                f"no robots.txt ({status})",
            )

        # 5xx, or no response at all. The host cannot tell us its rules, so we
        # do not get to assume they are permissive. Short TTL so this recovers.
        METRICS.incr("robots.unavailable")
        log.warning("robots.unavailable", origin=origin, status=status)
        return _Entry(
            _restrictive(),
            time.monotonic(),
            self.failure_ttl_s,
            f"robots.txt unavailable ({status or 'no response'})",
        )

    async def check(self, url: str) -> Verdict:
        """Whether `url` may be fetched, and how fast this origin allows."""
        origin = origin_of(url)
        entry = await self._entry(origin)
        parser = entry.parser

        allowed = parser.can_fetch(self.user_agent, url)
        delay = parser.crawl_delay(self.user_agent)
        rate = parser.request_rate(self.user_agent)

        # Both directives express a ceiling. Take the lower, since a host that
        # publishes both means the stricter one.
        caps: list[float] = []
        if delay:
            caps.append(1.0 / float(delay))
        if rate is not None and rate.seconds > 0:
            caps.append(rate.requests / rate.seconds)
        max_rps = min(caps) if caps else None

        if not allowed:
            METRICS.incr("robots.disallowed")
            log.info("robots.disallowed", url=url, reason=entry.reason)

        return Verdict(
            allowed=allowed,
            reason=entry.reason if allowed else f"disallowed by {entry.reason or 'robots.txt'}",
            crawl_delay=float(delay) if delay else None,
            max_rps=max_rps,
        )

    def snapshot(self) -> dict[str, str]:
        """Which origins have been read, and what they said. For /metrics."""
        return {origin: entry.reason for origin, entry in self._entries.items()}
