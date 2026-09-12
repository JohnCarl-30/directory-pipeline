"""The one HTTP client every outbound call goes through.

Composition order matters and is the whole point of this module:

    circuit breaker  ->  rate limiter  ->  request  ->  classify  ->  backoff

The breaker comes first so an already-failing host costs nothing. The limiter
comes before the request so we never *send* over budget (limiting after the
fact just means getting 429'd politely). Classification decides retryable vs
terminal, and only retryable errors reach the backoff loop.

Retries use exponential backoff with full jitter. Jitter is not cosmetic: a
batch of 50 coroutines that all hit a 503 at once will, without it, retry in
lockstep forever and keep the host down.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..observability import METRICS, get_logger
from .circuit import BreakerRegistry, CircuitOpenError
from .rate_limit import HostRateLimiter

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}

# Rotated so a single fingerprint doesn't accumulate a reputation across a crawl.
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]


class FetchError(RuntimeError):
    """Raised after retries are exhausted, or immediately for terminal errors."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    via_proxy: str | None
    attempts: int


class ProxyPool:
    """Round-robin proxy rotation with per-proxy failure ejection.

    A proxy that fails repeatedly is parked rather than removed, so a transient
    provider outage doesn't permanently shrink the pool.
    """

    def __init__(self, proxies: list[str], eject_after: int = 3) -> None:
        self._all = list(proxies)
        self._cycle = itertools.cycle(self._all) if self._all else None
        self._failures: dict[str, int] = {}
        self._eject_after = eject_after

    @property
    def enabled(self) -> bool:
        return bool(self._all)

    def next(self) -> str | None:
        if not self._cycle:
            return None
        for _ in range(len(self._all)):
            candidate = next(self._cycle)
            if self._failures.get(candidate, 0) < self._eject_after:
                return candidate
        # Every proxy is parked -- reset and try again rather than go direct,
        # which would leak the worker's real egress IP.
        self._failures.clear()
        return next(self._cycle)

    def report(self, proxy: str | None, ok: bool) -> None:
        if not proxy:
            return
        if ok:
            self._failures.pop(proxy, None)
        else:
            self._failures[proxy] = self._failures.get(proxy, 0) + 1


class ResilientClient:
    """Async HTTP client with limiting, breaking, retries and proxy rotation."""

    def __init__(
        self,
        settings: Settings,
        *,
        rps: float | None = None,
        burst: int | None = None,
        max_attempts: int = 4,
        base_backoff_s: float = 0.5,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.settings = settings
        self.limiter = HostRateLimiter(
            rps if rps is not None else settings.crawl_rps,
            burst if burst is not None else settings.crawl_burst,
        )
        self.breakers = BreakerRegistry()
        self.proxies = ProxyPool(settings.proxy_pool)
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._client_guard = asyncio.Lock()

    async def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        key = proxy or "__direct__"
        if key not in self._clients:
            async with self._client_guard:
                if key not in self._clients:
                    self._clients[key] = httpx.AsyncClient(
                        timeout=httpx.Timeout(self.settings.request_timeout_s),
                        follow_redirects=True,
                        proxy=proxy,
                        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                    )
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def __aenter__(self) -> ResilientClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_backoff_s)
        ceiling = min(self.max_backoff_s, self.base_backoff_s * (2 ** (attempt - 1)))
        return random.uniform(0, ceiling)  # full jitter

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, **kwargs: object
    ) -> Response:
        return await self.request("GET", url, headers=headers, **kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> Response:
        host = urlsplit(url).netloc
        breaker = self.breakers.get(host)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                await breaker.before_request(host)
            except CircuitOpenError as exc:
                METRICS.incr("http.circuit_open", host=host)
                raise FetchError(str(exc), retryable=True) from exc

            waited = await self.limiter.acquire(host)
            if waited > 0:
                METRICS.observe("http.rate_limit_wait_s", waited)

            proxy = self.proxies.next()
            client = await self._client_for(proxy)
            request_headers = {
                "User-Agent": random.choice(_UA_POOL),
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                **(headers or {}),
            }

            try:
                resp = await client.request(method, url, headers=request_headers, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                self.proxies.report(proxy, ok=False)
                await breaker.on_failure()
                METRICS.incr("http.transport_error", host=host)
                if attempt == self.max_attempts:
                    break
                await asyncio.sleep(self._backoff(attempt, None))
                continue

            METRICS.incr("http.response", host=host, status=str(resp.status_code))

            if resp.status_code in RETRYABLE_STATUS:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                if resp.status_code == 429:
                    # Slow every sibling coroutine on this host, not just this one.
                    await self.limiter.penalize(host, retry_after or 5.0)
                self.proxies.report(proxy, ok=False)
                await breaker.on_failure()
                last_error = FetchError(
                    f"{resp.status_code} from {url}", status=resp.status_code, retryable=True
                )
                if attempt == self.max_attempts:
                    break
                delay = self._backoff(attempt, retry_after)
                log.warning(
                    "http.retry",
                    url=url,
                    status=resp.status_code,
                    attempt=attempt,
                    sleep_s=round(delay, 2),
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                # 4xx other than the retryable set: our request is wrong, not theirs.
                self.proxies.report(proxy, ok=True)
                await breaker.on_success()
                raise FetchError(
                    f"{resp.status_code} from {url}", status=resp.status_code, retryable=False
                )

            self.proxies.report(proxy, ok=True)
            await breaker.on_success()
            return Response(
                url=str(resp.url),
                status=resp.status_code,
                text=resp.text,
                headers=dict(resp.headers),
                via_proxy=proxy,
                attempts=attempt,
            )

        raise FetchError(
            f"exhausted {self.max_attempts} attempts for {url}: {last_error}",
            status=getattr(last_error, "status", None),
            retryable=True,
        ) from last_error


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to jittered backoff
